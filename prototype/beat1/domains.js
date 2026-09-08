/*
 * SFDC24 — BEAT 1, domain lookup
 * claude-code-cli, 2026-09-08 · docs/DOMAINS-AND-HOSTING.md
 *
 * WHAT THIS IS
 *   The visitor describes what they want built. Names for it are proposed and
 *   checked for real availability and price, in the same breath as the sketch.
 *   That is the only part of the domain research that needs no decision from
 *   Mr. Salam: it moves no money, registers nothing, and creates no liability.
 *
 * WHAT THIS IS NOT
 *   It does not register. Registration waits on the ownership question in
 *   docs/DOMAINS-AND-HOSTING.md, which is his and not mine.
 *
 * THE BOUNDARY THAT MATTERS
 *   The description is VISITOR TEXT (L-57): data, never instructions. Here it
 *   becomes a DNS label that is then interpolated into a registrar's URL. Two
 *   distinct things can go wrong and both are handled below rather than hoped
 *   about:
 *
 *     1. Query injection. NameSilo's API takes the key IN THE URL:
 *          ...checkRegisterAvailability?version=1&key=KEY&domains=a.com,b.com
 *        A "domain" containing & or = could append parameters to that request.
 *        So labels are rebuilt from an allowlist of characters, never escaped
 *        and never trusted. Anything that does not survive the rebuild is
 *        dropped rather than repaired.
 *
 *     2. The key leaking outward. Any error text from a provider may be shown
 *        to a visitor or written to the board, and the key is in the URL that
 *        the provider will happily quote back in a message. redact() runs over
 *        everything this module returns.
 *
 * TESTS
 *   node tests/test_domain_lookup.js — no network, no key, fixtures only.
 */
'use strict';

// The registry of things we will offer. Deliberately short. A TLD we have not
// thought about is a TLD whose rules, price and restrictions we do not know --
// .ca alone carries Canadian Presence Requirements that can invalidate a
// registration after the fact (see docs/DOMAINS-AND-HOSTING.md).
const TLDS = ['com', 'ca', 'io', 'dev', 'app', 'net', 'org', 'co'];

// Per RFC 1035 / 1123: letters, digits, hyphen; not leading or trailing hyphen;
// 63 octets max per label. `xn--` is the one legal double-hyphen-at-position-3
// form, and we do not mint punycode ourselves, so it is rejected on input.
const MAX_LABEL = 63;

function str_(v) {
  // String(x) throws on an object whose toString is not callable. A visitor's
  // JSON body can carry {"description":{"toString":1}}. Found by a test written
  // to attack this repo's own code; the same guard is in sketch.js and build.js.
  if (v === null || v === undefined) return '';
  if (typeof v === 'string') return v;
  try { return String(v); } catch (e) { return ''; }
}

/**
 * Rebuild an arbitrary string as a legal DNS label, or return '' if it cannot
 * be one. REBUILD, not sanitise: every output character is chosen from the
 * allowlist below, so nothing from the input survives except by matching it.
 */
function toLabel(input) {
  const src = str_(input).toLowerCase().trim();

  // Punycode in, punycode out is not something this does. Folding xn--80ak6aa92e
  // down to xn-80ak6aa92e would silently produce a DIFFERENT domain and then
  // report on its availability, which is worse than refusing.
  if (src.startsWith('xn--')) return '';

  // NFKD splits an accented letter into base + combining mark. Without dropping
  // the marks, "Montréal" walks the allowlist as m-o-n-t-r-e-[mark]-a-l and the
  // mark becomes a hyphen: "montre-al". Caught by the test, not by reading.
  const raw = src.normalize('NFKD').replace(/[\u0300-\u036f]/g, '');

  let out = '';
  for (const ch of raw) {
    if ((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9')) out += ch;
    else if (out.length && out[out.length - 1] !== '-') out += '-';
    // Everything else -- &, =, ?, /, %, quotes, control characters, emoji,
    // anything non-ASCII that NFKD did not fold -- contributes nothing.
  }
  while (out.endsWith('-')) out = out.slice(0, -1);
  while (out.startsWith('-')) out = out.slice(1);
  if (!out.length || out.length > MAX_LABEL) return '';
  if (/^\d+$/.test(out)) return '';   // an all-digit label reads as an IP fragment
  // By construction a run of separators collapses to ONE hyphen, so `--` cannot
  // appear and no output can be punycode-shaped. Asserted rather than assumed,
  // because the first version of this function checked for `--` on the output
  // and that check could never fire.
  if (out.indexOf('--') !== -1) return '';
  return out;
}

/** Words too generic to make a name out of, and words we should never propose. */
const STOP = new Set([
  'a', 'an', 'and', 'the', 'for', 'with', 'that', 'this', 'my', 'our', 'we',
  'i', 'to', 'of', 'in', 'on', 'it', 'is', 'be', 'want', 'need', 'like',
  'app', 'site', 'website', 'page', 'thing', 'something', 'new', 'make',
  'build', 'create', 'help', 'please', 'can', 'you'
]);

/**
 * Propose candidate labels from a plain description. Deterministic and offline:
 * the same words in, the same names out, no model call and no network.
 *
 * This is intentionally dumb. A generative naming pass belongs behind the
 * Cloudflare `search` endpoint (docs/DOMAINS-AND-HOSTING.md) when there is a
 * key for it. Until then a predictable shortlist beats an invented one, because
 * every name here has to survive being read back to the visitor.
 */
function suggestLabels(description, opts) {
  const limit = (opts && opts.limit) || 8;
  const words = str_(description)
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .map((w) => w.trim())
    .filter((w) => w.length > 2 && w.length < 20 && !STOP.has(w));

  const seen = new Set();
  const out = [];
  const push = (v) => {
    const l = toLabel(v);
    if (l && !seen.has(l)) { seen.add(l); out.push(l); }
  };

  // Pairs first: two real words from what they said reads like a name. Single
  // words next, as fallbacks. Nothing invented, nothing bolted on -- no
  // "-hq", no "get-", no "-ly". Those read as filler and they know it.
  for (let i = 0; i < words.length - 1 && out.length < limit * 2; i++) {
    push(words[i] + words[i + 1]);
    push(words[i] + '-' + words[i + 1]);
  }
  for (let i = 0; i < words.length && out.length < limit * 2; i++) push(words[i]);

  return out.slice(0, limit);
}

/** Full domains from labels × requested TLDs, capped so one call stays one call. */
function candidates(labels, tlds, cap) {
  const use = (tlds && tlds.length ? tlds : ['com', 'ca'])
    .map((t) => toLabel(t))
    .filter((t) => TLDS.indexOf(t) !== -1);
  const max = cap || 24;
  const out = [];
  for (const l of labels) {
    for (const t of use) {
      if (out.length >= max) return out;
      const name = l + '.' + t;
      if (name.length <= 253) out.push(name);
    }
  }
  return out;
}

/** Remove anything key-shaped from text that may be shown or logged. */
function redact(text, secrets) {
  let s = str_(text);
  for (const secret of (secrets || [])) {
    const v = str_(secret);
    if (v.length >= 8) s = s.split(v).join('[redacted]');
  }
  // Belt and braces: a provider echoing our request URL back in an error.
  s = s.replace(/([?&](?:key|api_key|token|apikey)=)[^&\s"']+/gi, '$1[redacted]');
  return s;
}

/* ---------------------------------------------------------------------------
 * Providers.
 *
 * A provider is { name, buildRequest(domains, cfg) -> {url, headers},
 *                 parse(bodyText) -> [{domain, available, price, currency}] }
 * The transport is injected, so every test below runs with no network and no
 * key, and so a provider can be swapped when the ownership decision lands.
 * ------------------------------------------------------------------------- */

const namesilo = {
  name: 'namesilo',
  buildRequest(domains, cfg) {
    const key = str_(cfg && cfg.apiKey);
    if (!key) throw new Error('namesilo: no apiKey');
    // Each domain has already been rebuilt by toLabel + candidates. Assert it
    // again here rather than trust the caller: this is the string that becomes
    // part of a URL carrying our key.
    for (const d of domains) {
      if (!/^[a-z0-9-]{1,63}\.[a-z]{2,24}$/.test(d)) {
        throw new Error('namesilo: refusing to send an unsafe domain');
      }
    }
    return {
      url: 'https://www.namesilo.com/api/checkRegisterAvailability'
        + '?version=1&type=json&key=' + encodeURIComponent(key)
        + '&domains=' + encodeURIComponent(domains.join(',')),
      headers: { Accept: 'application/json' },
      secrets: [key]
    };
  },
  parse(body) {
    const j = JSON.parse(str_(body));
    const reply = (j && j.reply) || {};
    const rows = [];
    const take = (node, available) => {
      if (!node) return;
      const list = Array.isArray(node.domain) ? node.domain
        : (node.domain ? [node.domain] : (Array.isArray(node) ? node : [node]));
      for (const d of list) {
        const domain = typeof d === 'string' ? d : str_(d && d.domain);
        if (!domain) continue;
        const price = (d && d.price !== undefined) ? Number(d.price) : null;
        rows.push({
          domain,
          available,
          price: Number.isFinite(price) ? price : null,
          currency: 'USD'
        });
      }
    };
    take(reply.available, true);
    take(reply.unavailable, false);
    return rows;
  }
};

const cloudflare = {
  name: 'cloudflare',
  buildRequest(domains, cfg) {
    const token = str_(cfg && cfg.apiToken);
    const account = str_(cfg && cfg.accountId);
    if (!token || !account) throw new Error('cloudflare: no apiToken/accountId');
    // The token rides in a header here, not the query string. That is the whole
    // reason to prefer this provider once there is an account for it.
    return {
      url: 'https://api.cloudflare.com/client/v4/accounts/'
        + encodeURIComponent(account) + '/registrar/domains/check?domains='
        + encodeURIComponent(domains.join(',')),
      headers: { Authorization: 'Bearer ' + token, Accept: 'application/json' },
      secrets: [token]
    };
  },
  parse(body) {
    const j = JSON.parse(str_(body));
    const list = (j && j.result) || [];
    return (Array.isArray(list) ? list : []).map((r) => ({
      domain: str_(r && r.name),
      available: !!(r && r.available),
      price: (r && Number.isFinite(Number(r.price))) ? Number(r.price) : null,
      currency: str_(r && r.currency) || 'USD'
    })).filter((r) => r.domain);
  }
};

const PROVIDERS = { namesilo, cloudflare };

/**
 * Look up availability. `transport(url, headers) -> Promise<string>` is
 * injected; nothing in this module opens a socket by itself.
 *
 * Never throws a provider's raw error outward: an availability check failing is
 * a normal event and must degrade to "we could not check" rather than taking
 * the sketch down with it.
 */
async function lookup(description, opts) {
  const o = opts || {};
  const provider = PROVIDERS[str_(o.provider) || 'namesilo'];
  if (!provider) return { ok: false, checked: false, reason: 'unknown provider', results: [] };

  const labels = suggestLabels(description, { limit: o.limit || 6 });
  const domains = candidates(labels, o.tlds, o.cap || 12);
  if (!domains.length) {
    // Not an error. Some descriptions genuinely contain no usable word, and
    // saying so beats offering a name nobody asked for.
    return { ok: true, checked: false, reason: 'no usable name in that description', suggestions: [], results: [] };
  }
  if (!o.transport) {
    // The shape is still useful without a key: the visitor sees the candidate
    // names, just not whether they are free. Do not pretend they are.
    return { ok: true, checked: false, reason: 'no lookup key configured', suggestions: domains, results: [] };
  }

  let req;
  try {
    req = provider.buildRequest(domains, o.config || {});
  } catch (e) {
    return { ok: true, checked: false, reason: redact(e.message, []), suggestions: domains, results: [] };
  }

  try {
    const body = await o.transport(req.url, req.headers);
    const results = provider.parse(body);
    return { ok: true, checked: true, provider: provider.name, suggestions: domains, results };
  } catch (e) {
    return {
      ok: true, checked: false, provider: provider.name,
      reason: redact(e && e.message, req.secrets),
      suggestions: domains, results: []
    };
  }
}

module.exports = {
  TLDS, toLabel, suggestLabels, candidates, redact, lookup,
  providers: PROVIDERS, _str: str_
};
