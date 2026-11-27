/**
 * Server-to-server sender for the SFDC24 Studio controller: the sign-in code,
 * and (kind "summary") the working-session PDF a visitor asked for at the end
 * of a conversation. Both are HMAC-signed by the controller; the summary
 * signature covers the SHA-256 of the exact PDF attached.
 *
 * Two more PDF kinds, for a one-page plan drafted after a call: "plan_review"
 * (the operator's review copy, with a link to the page where it is approved;
 * operator allowlist only) and "plan" (the approved copy, to the person it was
 * written for). Each is signed over its own kind, so no kind's signature is
 * ever accepted as another's.
 *
 * Kind "feedback": a one-page digest of what a guest said when asked for
 * feedback after a trial session, to the operator allowlist only (no property
 * opens it to anyone else).
 */
var STUDIO_EMAIL_CODE_MAX_CHARS = 2048;
var STUDIO_EMAIL_SUMMARY_MAX_CHARS = 4000000;
var STUDIO_EMAIL_PDF_MAX_CHARS = 3900000;
var STUDIO_EMAIL_PDF_KINDS = ['summary', 'plan_review', 'plan', 'feedback'];

function doPost(e) {
  try {
    if (!e || !e.postData || typeof e.postData.contents !== 'string' ||
        e.postData.contents.length > STUDIO_EMAIL_SUMMARY_MAX_CHARS) {
      return studioEmailResponse_(false);
    }

    var request = JSON.parse(e.postData.contents);
    var kind = (!!request && typeof request === 'object' && !Array.isArray(request) &&
        STUDIO_EMAIL_PDF_KINDS.indexOf(request.kind) !== -1) ? request.kind : '';
    var summary = kind === 'summary';
    if (!kind && e.postData.contents.length > STUDIO_EMAIL_CODE_MAX_CHARS) {
      return studioEmailResponse_(false);
    }
    if (kind ? !studioEmailValidPdfRequest_(request, kind) : !studioEmailValidRequest_(request)) {
      return studioEmailResponse_(false);
    }

    var properties = PropertiesService.getScriptProperties();
    var secret = properties.getProperty('STUDIO_EMAIL_SENDER_SECRET');
    var rawAllowlist = properties.getProperty('STUDIO_OPERATOR_EMAILS');
    if (typeof secret !== 'string' || secret.length < 32 ||
        typeof rawAllowlist !== 'string' || !rawAllowlist) {
      return studioEmailResponse_(false);
    }
    var allowed = rawAllowlist.split(',').map(function (entry) { return entry.trim(); });
    if (!allowed.every(function (entry) {
      return entry && entry === entry.toLowerCase() &&
          /^[^\s@]+@[^\s@]+$/.test(entry);
    })) {
      return studioEmailResponse_(false);
    }
    // A summary or an approved plan may go to someone off the allowlist only
    // when the owner opens that kind on purpose, each with its own property.
    // The sign-in code, the plan's review copy and feedback always require the allowlist.
    var anyRecipient = (summary &&
        properties.getProperty('STUDIO_SUMMARY_ANY_RECIPIENT') === 'true') ||
        (kind === 'plan' && properties.getProperty('STUDIO_PLAN_ANY_RECIPIENT') === 'true');
    if (!anyRecipient && allowed.indexOf(request.email) === -1) {
      return studioEmailResponse_(false);
    }

    var canonical = kind
        ? studioEmailCanonical_(request, kind)
        : [request.timestamp, request.nonce, request.email, request.code].join('\n');
    var signedBytes = Utilities.computeHmacSha256Signature(
      canonical, secret, Utilities.Charset.UTF_8);
    if (!studioEmailEqual_(studioEmailHex_(signedBytes), request.signature)) {
      return studioEmailResponse_(false);
    }
    var pdfBytes = null;
    if (kind) {
      // The attachment must be exactly the PDF the controller signed.
      pdfBytes = Utilities.base64Decode(request.pdf);
      var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, pdfBytes);
      if (!studioEmailEqual_(studioEmailHex_(digest), request.pdf_sha256)) {
        return studioEmailResponse_(false);
      }
      if (pdfBytes.length < 5 || pdfBytes[0] !== 37 || pdfBytes[1] !== 80 ||
          pdfBytes[2] !== 68 || pdfBytes[3] !== 70) {
        return studioEmailResponse_(false);  // not %PDF
      }
    }

    // CacheService can evict entries before their TTL. Keep a small durable
    // nonce ledger as well, so early eviction does not permit a replay.
    // Reservation precedes delivery: an ambiguous failure cannot send twice.
    var lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      // A request can cross the freshness boundary while waiting for the lock.
      // Use this same time for freshness and pruning: cache.get can also stall.
      var now = Date.now() / 1000;
      if (!studioEmailFresh_(request.timestamp, now)) return studioEmailResponse_(false);
      var cache = CacheService.getScriptCache();
      var nonceKey = 'studio-email-nonce-' + request.nonce;
      if (cache.get(nonceKey) !== null) return studioEmailResponse_(false);
      var ledgerKey = 'STUDIO_EMAIL_USED_NONCES';
      var ledger = JSON.parse(properties.getProperty(ledgerKey) || '{}');
      if (!ledger || typeof ledger !== 'object' || Array.isArray(ledger)) {
        return studioEmailResponse_(false);
      }
      Object.keys(ledger).forEach(function (nonce) {
        if (typeof ledger[nonce] !== 'number' || ledger[nonce] < now) {
          delete ledger[nonce];
        }
      });
      if (Object.prototype.hasOwnProperty.call(ledger, request.nonce)) {
        return studioEmailResponse_(false);
      }
      // Retain through the 120-second acceptance window, the 10-second lock
      // wait, and one second for integer-second rounding.
      ledger[request.nonce] = Number(request.timestamp) + 131;
      properties.setProperty(ledgerKey, JSON.stringify(ledger));
      cache.put(nonceKey, '1', 300);
    } finally {
      lock.releaseLock();
    }

    if (summary) {
      MailApp.sendEmail(
        request.email,
        'Your SFDC24 working session',
        'Attached is your working session from sfdc24.com: what you asked for, ' +
            'what was built, and the final design.',
        { name: 'SFDC24', attachments: [
          Utilities.newBlob(pdfBytes, 'application/pdf', 'SFDC24-working-session.pdf')] }
      );
      return studioEmailResponse_(true);
    }
    if (kind === 'plan_review') {
      MailApp.sendEmail(
        request.email,
        'Review: a proof of concept quote' + (request.label ? ' for ' + request.label : ''),
        'A proof of concept quote from your call is ready for your review. Your review ' +
            'copy is attached; it shows the suggested price of the paid follow-on, ' +
            'which the guest never sees.\n\nReview it, edit it and send it here:\n' +
            request.review_url + '\n\nNothing reaches the guest until you press ' +
            'Approve and send.',
        { name: 'SFDC24', attachments: [
          Utilities.newBlob(pdfBytes, 'application/pdf', 'SFDC24-proof-of-concept-quote-review.pdf')] }
      );
      return studioEmailResponse_(true);
    }
    if (kind === 'feedback') {
      MailApp.sendEmail(
        request.email,
        'Guest feedback from a solo demo' + (request.label ? ': ' + request.label : ''),
        'A guest left feedback at the end of a solo demo session. It is attached, ' +
            'with the session time. It was logged for you only; nothing was done ' +
            'with it in the call.',
        { name: 'SFDC24', attachments: [
          Utilities.newBlob(pdfBytes, 'application/pdf', 'SFDC24-guest-feedback.pdf')] }
      );
      return studioEmailResponse_(true);
    }
    if (kind === 'plan') {
      MailApp.sendEmail(
        request.email,
        'Your proof of concept quote from SFDC24',
        (request.label ? 'Hi ' + request.label + ',\n\n' : '') +
            'Thank you for your time on the call. Attached is your quote for a proof ' +
            'of concept: the flow we will build for you, what you will be able to try, ' +
            'its price and turnaround, and what comes next.\n\n' +
            'Reply to this email to say go.\n\nSFDC24',
        { name: 'SFDC24', attachments: [
          Utilities.newBlob(pdfBytes, 'application/pdf', 'SFDC24-proof-of-concept-quote.pdf')] }
      );
      return studioEmailResponse_(true);
    }
    MailApp.sendEmail(
      request.email,
      'Your SFDC24 Studio verification code',
      'Your SFDC24 Studio verification code is ' + request.code +
          '. It expires in 10 minutes. If you did not request it, ignore this email.'
    );
    return studioEmailResponse_(true);
  } catch (ignored) {
    // Apps Script exceptions can contain request data. Never log or return them.
    return studioEmailResponse_(false);
  }
}

function studioEmailValidRequest_(request) {
  if (!request || typeof request !== 'object' || Array.isArray(request)) return false;
  var keys = Object.keys(request).sort();
  if (keys.join(',') !== 'code,email,nonce,signature,timestamp') return false;
  if (!keys.every(function (key) { return typeof request[key] === 'string'; })) return false;
  if (!/^[1-9][0-9]{9}$/.test(request.timestamp)) return false;
  if (!studioEmailFresh_(request.timestamp, Date.now() / 1000)) return false;
  if (!/^[0-9a-f]{32}$/.test(request.nonce)) return false;
  if (request.email.length > 320 || request.email !== request.email.toLowerCase() ||
      !/^[^\s@]+@[^\s@]+$/.test(request.email)) return false;
  if (!/^[0-9]{6}$/.test(request.code)) return false;
  return /^[0-9a-f]{64}$/.test(request.signature);
}

// The exact fields of each PDF kind, sorted, and what each signs after its
// kind, timestamp, nonce, email and PDF digest.
var STUDIO_EMAIL_PDF_FIELDS = {
  summary: 'email,kind,nonce,pdf,pdf_sha256,signature,timestamp',
  plan_review: 'email,kind,label,nonce,pdf,pdf_sha256,review_url,signature,timestamp',
  plan: 'email,kind,label,nonce,pdf,pdf_sha256,signature,timestamp',
  feedback: 'email,kind,label,nonce,pdf,pdf_sha256,signature,timestamp'
};
var STUDIO_EMAIL_PDF_SIGNED = { summary: [], plan_review: ['review_url', 'label'], plan: ['label'], feedback: ['label'] };
// A label is a short plain name for the subject or greeting: no control
// character, no markup and no address.
var STUDIO_EMAIL_LABEL = /^[^\u0000-\u001f\u007f<>@]{0,80}$/;
// The review page's link: https only, a host and a short plain path.
var STUDIO_EMAIL_REVIEW_URL = /^https:\/\/[A-Za-z0-9.-]+(?::[0-9]+)?\/[A-Za-z0-9\/_.#?=&-]{0,200}$/;

function studioEmailCanonical_(request, kind) {
  return [kind, request.timestamp, request.nonce, request.email, request.pdf_sha256]
      .concat(STUDIO_EMAIL_PDF_SIGNED[kind].map(function (key) { return request[key]; }))
      .join('\n');
}

function studioEmailValidPdfRequest_(request, kind) {
  if (!studioEmailValidSummary_(request, STUDIO_EMAIL_PDF_FIELDS[kind])) return false;
  if (kind !== 'summary' && !STUDIO_EMAIL_LABEL.test(request.label)) return false;
  if (kind === 'plan_review' && !STUDIO_EMAIL_REVIEW_URL.test(request.review_url)) return false;
  return true;
}

function studioEmailValidSummary_(request, fields) {
  var keys = Object.keys(request).sort();
  if (keys.join(',') !== (fields || STUDIO_EMAIL_PDF_FIELDS.summary)) return false;
  if (!keys.every(function (key) { return typeof request[key] === 'string'; })) return false;
  if (!/^[1-9][0-9]{9}$/.test(request.timestamp)) return false;
  if (!studioEmailFresh_(request.timestamp, Date.now() / 1000)) return false;
  if (!/^[0-9a-f]{32}$/.test(request.nonce)) return false;
  if (request.email.length > 320 || request.email !== request.email.toLowerCase() ||
      !/^[^\s@]+@[^\s@]+$/.test(request.email)) return false;
  if (!/^[0-9a-f]{64}$/.test(request.pdf_sha256)) return false;
  if (request.pdf.length === 0 || request.pdf.length > STUDIO_EMAIL_PDF_MAX_CHARS ||
      request.pdf.length % 4 !== 0 || !/^[A-Za-z0-9+\/]+={0,2}$/.test(request.pdf)) return false;
  return /^[0-9a-f]{64}$/.test(request.signature);
}

function studioEmailHex_(bytes) {
  return bytes.map(function (byte) {
    return ('0' + (byte & 255).toString(16)).slice(-2);
  }).join('');
}

function studioEmailFresh_(timestamp, now) {
  return Math.abs(now - Number(timestamp)) <= 120;
}

function studioEmailEqual_(left, right) {
  // Fixed-length hex strings have already been validated. Compare every byte.
  var difference = left.length ^ right.length;
  for (var i = 0; i < 64; i++) {
    difference |= left.charCodeAt(i) ^ right.charCodeAt(i);
  }
  return difference === 0;
}

function studioEmailResponse_(ok) {
  return ContentService.createTextOutput(JSON.stringify({ ok: ok }))
      .setMimeType(ContentService.MimeType.JSON);
}
