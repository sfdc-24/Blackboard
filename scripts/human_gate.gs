/**
 * SFDC24 — HUMAN GATE (server half)
 * claude-code-cli, 2026-09-03. For the Governor Page reception chat.
 *
 * THE PROBLEM
 *   The reception chat is an anonymous public endpoint that calls a paid LLM.
 *   Every request a bot makes costs real money. Left open, a single script can
 *   drain a day's budget in seconds.
 *
 * THE STRATEGY — four layers, cheapest first
 *   1. HONEYPOT     free, catches naive form-fillers
 *   2. DWELL TIME   free, catches anything that submits faster than typing
 *   3. PROOF OF WORK  invisible to humans, expensive at volume for bots
 *   4. CAPS         already implemented (CHAT_DAILY_CAP / CHAT_SESSION_CAP)
 *
 *   Layers 1–3 raise the attacker's cost. Layer 4 bounds YOUR cost. Keep both:
 *   a determined attacker beats 1–3, and only the caps stop the bill.
 *
 * WHY PROOF OF WORK AND NOT A CAPTCHA
 *   A CAPTCHA taxes the human and lets the bot through for about $0.001 via a
 *   solving service. Proof of work inverts that: the human pays nothing they
 *   notice (~300ms, while they are already typing) and the bot pays CPU on
 *   every single request. It is also no third-party script, no tracking, no
 *   cookie banner, and it works with JS-only clients — which a bot is.
 *
 *   It is NOT unbeatable. A patient attacker with CPU gets through. The point
 *   is to make cheap high-volume abuse uneconomic, and let the caps handle the
 *   rest. Anyone who tells you a gate is unbeatable is selling something.
 *
 * SETUP
 *   Project Settings > Script Properties:
 *     GATE_SECRET  a long random string (rotate any time; only invalidates
 *                  in-flight challenges, which are 5 minutes old at most)
 *   Optional:
 *     GATE_BITS    difficulty in leading zero bits (default 18)
 *     GATE_OFF     set to "1" to disable the gate entirely in an emergency
 */

var GATE_TTL_MS      = 5 * 60 * 1000;  // a challenge is good for 5 minutes
var GATE_MIN_DWELL_MS = 1200;          // nobody reads and types faster
var GATE_DEFAULT_BITS = 18;            // ~250ms on a phone, ~80ms on a laptop

function gateProps_() {
  return PropertiesService.getScriptProperties();
}

function gateBits_() {
  var b = parseInt(gateProps_().getProperty('GATE_BITS'), 10);
  return (b >= 8 && b <= 26) ? b : GATE_DEFAULT_BITS;
}

/**
 * Issue a challenge. Stateless: the token carries its own timestamp and an
 * HMAC, so nothing needs storing until a solution comes back. Call this from
 * doGet when the reception page loads.
 */
function issueGateChallenge_() {
  var secret = gateProps_().getProperty('GATE_SECRET');
  if (!secret) throw new Error('GATE_SECRET not set in Script Properties');

  var body = Date.now() + '.' + Utilities.getUuid();
  return {
    challenge: body + '.' + gateSign_(body, secret),
    bits: gateBits_()
  };
}

function gateSign_(body, secret) {
  var raw = Utilities.computeHmacSha256Signature(body, secret);
  return raw.map(function (b) {
    return ('0' + (b & 0xFF).toString(16)).slice(-2);
  }).join('');
}

/**
 * Verify a submission. Returns { ok: true } or { ok:false, reason:'...' }.
 *
 * The reason strings are deliberately generic in what they reveal to the
 * caller — an attacker probing which layer caught them learns nothing useful
 * from "rejected". Log the specific reason server-side instead.
 */
function verifyHumanGate_(sub) {
  if (gateProps_().getProperty('GATE_OFF') === '1') return { ok: true, bypassed: true };

  sub = sub || {};

  // --- Layer 1: honeypot -------------------------------------------------
  // The form carries a field named like something a bot wants to fill
  // ("website"), hidden from humans by CSS and aria-hidden. Any value at all
  // means it was not a person.
  if (sub.website) return { ok: false, reason: 'honeypot', log: 'honeypot filled' };

  // --- Layer 2: dwell time ----------------------------------------------
  // Time between the page issuing the challenge and the message arriving.
  var dwell = Number(sub.dwellMs || 0);
  if (!(dwell >= GATE_MIN_DWELL_MS)) {
    return { ok: false, reason: 'too_fast', log: 'dwell ' + dwell + 'ms' };
  }

  // --- Layer 3: proof of work -------------------------------------------
  var token = String(sub.challenge || '');
  var parts = token.split('.');
  if (parts.length !== 3) return { ok: false, reason: 'bad_token', log: 'malformed' };

  var body = parts[0] + '.' + parts[1];
  var secret = gateProps_().getProperty('GATE_SECRET');
  if (!secret) throw new Error('GATE_SECRET not set in Script Properties');

  // Constant-ish time compare. Apps Script has no timing-safe primitive, and
  // an HMAC over a 5-minute-lived token is not a realistic timing target, but
  // compare the whole string rather than bailing on the first mismatch.
  var expect = gateSign_(body, secret);
  if (!gateEq_(expect, parts[2])) {
    return { ok: false, reason: 'bad_token', log: 'signature mismatch' };
  }

  var issued = Number(parts[0]);
  var age = Date.now() - issued;
  if (!(age >= 0 && age <= GATE_TTL_MS)) {
    return { ok: false, reason: 'expired', log: 'age ' + age + 'ms' };
  }

  // Replay: a solved challenge is worth money, so it is worth stealing. One
  // use only. CacheService is the right store — it expires on its own and
  // costs nothing to maintain.
  var cache = CacheService.getScriptCache();
  var key = 'gate_' + parts[1];
  if (cache.get(key)) return { ok: false, reason: 'replay', log: 'token reused' };

  var bits = gateBits_();
  if (!gatePowOk_(token, String(sub.nonce || ''), bits)) {
    return { ok: false, reason: 'bad_work', log: 'pow failed at ' + bits + ' bits' };
  }

  cache.put(key, '1', Math.ceil(GATE_TTL_MS / 1000));
  return { ok: true };
}

function gateEq_(a, b) {
  if (a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= (a.charCodeAt(i) ^ b.charCodeAt(i));
  return diff === 0;
}

/**
 * SHA-256(challenge + ':' + nonce) must begin with `bits` zero bits.
 * Checked bit by bit rather than on a hex prefix so difficulty is tunable in
 * single steps — each extra bit doubles the attacker's cost, and jumping a
 * whole hex digit at a time (4 bits, 16x) is far too blunt.
 */
function gatePowOk_(challenge, nonce, bits) {
  if (!nonce) return false;
  var digest = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256, challenge + ':' + nonce, Utilities.Charset.UTF_8);

  var need = bits;
  for (var i = 0; i < digest.length && need > 0; i++) {
    var byte = digest[i] & 0xFF;
    if (need >= 8) {
      if (byte !== 0) return false;
      need -= 8;
    } else {
      return (byte >> (8 - need)) === 0;
    }
  }
  return need <= 0;
}

/**
 * Drop-in for the reception handler. Verify BEFORE spending a token on the
 * model — that ordering is the entire point.
 *
 *   var gate = verifyHumanGate_(req.gate);
 *   if (!gate.ok) {
 *     console.log('gate rejected: ' + gate.log);   // specific, server-side
 *     return json_({ ok:false, error:'Could not verify this request.' });
 *   }
 *   ... only now call the model ...
 */
