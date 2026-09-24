/** Server-to-server OTP sender for the SFDC24 Studio controller. */
function doPost(e) {
  try {
    if (!e || !e.postData || typeof e.postData.contents !== 'string' ||
        e.postData.contents.length > 2048) {
      return studioEmailResponse_(false);
    }

    var request = JSON.parse(e.postData.contents);
    if (!studioEmailValidRequest_(request)) return studioEmailResponse_(false);

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
    }) || allowed.indexOf(request.email) === -1) {
      return studioEmailResponse_(false);
    }

    var canonical = [request.timestamp, request.nonce, request.email, request.code].join('\n');
    var signedBytes = Utilities.computeHmacSha256Signature(
      canonical, secret, Utilities.Charset.UTF_8);
    var expected = signedBytes.map(function (byte) {
      return ('0' + (byte & 255).toString(16)).slice(-2);
    }).join('');
    if (!studioEmailEqual_(expected, request.signature)) return studioEmailResponse_(false);

    // CacheService can evict entries before their TTL. Keep a small durable
    // nonce ledger as well, so early eviction does not permit a replay.
    // Reservation precedes delivery: an ambiguous failure cannot send twice.
    var lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      var cache = CacheService.getScriptCache();
      var nonceKey = 'studio-email-nonce-' + request.nonce;
      if (cache.get(nonceKey) !== null) return studioEmailResponse_(false);
      var now = Math.floor(Date.now() / 1000);
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
      ledger[request.nonce] = Number(request.timestamp) + 121;
      properties.setProperty(ledgerKey, JSON.stringify(ledger));
      cache.put(nonceKey, '1', 300);
    } finally {
      lock.releaseLock();
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
  if (Math.abs(Date.now() / 1000 - Number(request.timestamp)) > 120) return false;
  if (!/^[0-9a-f]{32}$/.test(request.nonce)) return false;
  if (request.email.length > 320 || request.email !== request.email.toLowerCase() ||
      !/^[^\s@]+@[^\s@]+$/.test(request.email)) return false;
  if (!/^[0-9]{6}$/.test(request.code)) return false;
  return /^[0-9a-f]{64}$/.test(request.signature);
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
