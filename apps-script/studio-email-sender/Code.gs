/**
 * Server-to-server sender for the SFDC24 Studio controller: the sign-in code,
 * and (kind "summary") the working-session PDF a visitor asked for at the end
 * of a conversation. Both are HMAC-signed by the controller; the summary
 * signature covers the SHA-256 of the exact PDF attached.
 */
var STUDIO_EMAIL_CODE_MAX_CHARS = 2048;
var STUDIO_EMAIL_SUMMARY_MAX_CHARS = 4000000;
var STUDIO_EMAIL_PDF_MAX_CHARS = 3900000;

function doPost(e) {
  try {
    if (!e || !e.postData || typeof e.postData.contents !== 'string' ||
        e.postData.contents.length > STUDIO_EMAIL_SUMMARY_MAX_CHARS) {
      return studioEmailResponse_(false);
    }

    var request = JSON.parse(e.postData.contents);
    var summary = !!request && typeof request === 'object' && !Array.isArray(request) &&
        request.kind === 'summary';
    if (!summary && e.postData.contents.length > STUDIO_EMAIL_CODE_MAX_CHARS) {
      return studioEmailResponse_(false);
    }
    if (summary ? !studioEmailValidSummary_(request) : !studioEmailValidRequest_(request)) {
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
    // Client workspaces (the controller's registry): these addresses get the
    // sign-in code and their summary too. Optional; an empty or unset property
    // adds nobody, and one malformed entry refuses everything like the list above.
    var rawClients = properties.getProperty('STUDIO_CLIENT_EMAILS');
    if (typeof rawClients === 'string' && rawClients.trim()) {
      allowed = allowed.concat(rawClients.split(',').map(function (entry) { return entry.trim(); }));
    }
    if (!allowed.every(function (entry) {
      return entry && entry === entry.toLowerCase() &&
          /^[^\s@]+@[^\s@]+$/.test(entry);
    })) {
      return studioEmailResponse_(false);
    }
    // A summary may go to a public visitor only when the owner opens that on
    // purpose; the sign-in code always requires the allowlist.
    var anyRecipient = summary &&
        properties.getProperty('STUDIO_SUMMARY_ANY_RECIPIENT') === 'true';
    if (!anyRecipient && allowed.indexOf(request.email) === -1) {
      return studioEmailResponse_(false);
    }

    var canonical = summary
        ? ['summary', request.timestamp, request.nonce, request.email, request.pdf_sha256].join('\n')
        : [request.timestamp, request.nonce, request.email, request.code].join('\n');
    var signedBytes = Utilities.computeHmacSha256Signature(
      canonical, secret, Utilities.Charset.UTF_8);
    if (!studioEmailEqual_(studioEmailHex_(signedBytes), request.signature)) {
      return studioEmailResponse_(false);
    }
    var pdfBytes = null;
    if (summary) {
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

function studioEmailValidSummary_(request) {
  var keys = Object.keys(request).sort();
  if (keys.join(',') !== 'email,kind,nonce,pdf,pdf_sha256,signature,timestamp') return false;
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
