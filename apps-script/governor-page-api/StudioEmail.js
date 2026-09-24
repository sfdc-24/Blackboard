/**
 * Server-to-server OTP delivery for the SFDC24 Studio controller.
 *
 * This route is deliberately separate from GOVERNOR_PASS and from the
 * standalone Studio sender.  It is reached only through
 * POST /exec?action=studio-email and is disabled unless the deploying owner
 * explicitly sets all three dedicated Script Properties:
 *
 *   STUDIO_GOVERNOR_EMAIL_ENABLED   on
 *   STUDIO_GOVERNOR_EMAIL_SECRET    64 lowercase hexadecimal characters
 *   STUDIO_GOVERNOR_OPERATOR_EMAILS comma-separated lowercase addresses
 *
 * Rejections are intentionally opaque.  Never log the request: it contains a
 * live verification code and an email address.
 */
var STUDIO_EMAIL_ENABLED_KEY_ = 'STUDIO_GOVERNOR_EMAIL_ENABLED';
var STUDIO_EMAIL_SECRET_KEY_ = 'STUDIO_GOVERNOR_EMAIL_SECRET';
var STUDIO_EMAIL_ALLOWLIST_KEY_ = 'STUDIO_GOVERNOR_OPERATOR_EMAILS';
var STUDIO_EMAIL_STATE_KEY_ = 'STUDIO_GOVERNOR_EMAIL_STATE_V1';
var STUDIO_EMAIL_GLOBAL_WINDOW_ = 24 * 60 * 60;
var STUDIO_EMAIL_GLOBAL_LIMIT_ = 20;
var STUDIO_EMAIL_SUBJECT_WINDOW_ = 15 * 60;
var STUDIO_EMAIL_SUBJECT_LIMIT_ = 3;
var STUDIO_EMAIL_MONITOR_HEADROOM_ = 12;

function studioEmailHandlePost_(e) {
  try {
    if (!e || !e.postData || typeof e.postData.contents !== 'string' ||
        e.postData.contents.length > 2048) {
      return studioEmailResponse_(false);
    }

    var request = JSON.parse(e.postData.contents);
    if (!studioEmailValidRequest_(request)) return studioEmailResponse_(false);

    var properties = PropertiesService.getScriptProperties();
    if (properties.getProperty(STUDIO_EMAIL_ENABLED_KEY_) !== 'on') {
      return studioEmailResponse_(false);
    }
    var secret = properties.getProperty(STUDIO_EMAIL_SECRET_KEY_);
    if (typeof secret !== 'string' || !/^[0-9a-f]{64}$/.test(secret)) {
      return studioEmailResponse_(false);
    }

    // Authenticate the complete request before doing recipient-dependent work.
    // Otherwise an invalid signature can distinguish allowlist membership by
    // whether the HMAC primitive ran.
    var canonical = [request.timestamp, request.nonce, request.email, request.code].join('\n');
    var expected = studioEmailHmacHex_(canonical, secret);
    if (!studioEmailEqual_(expected, request.signature)) {
      return studioEmailResponse_(false);
    }

    var rawAllowlist = properties.getProperty(STUDIO_EMAIL_ALLOWLIST_KEY_);
    if (typeof rawAllowlist !== 'string' || !rawAllowlist) {
      return studioEmailResponse_(false);
    }
    var allowed = rawAllowlist.split(',').map(function (entry) {
      return entry.trim();
    });
    var seen = {};
    if (!allowed.every(function (entry) {
      if (!entry || entry !== entry.toLowerCase() || entry.length > 320 ||
          !/^[^\s@]+@[^\s@]+$/.test(entry) || seen[entry]) return false;
      seen[entry] = true;
      return true;
    }) || allowed.indexOf(request.email) === -1) {
      return studioEmailResponse_(false);
    }

    var now = Math.floor(Date.now() / 1000);
    var subject = studioEmailHmacHex_('studio-email-subject-v1\n' + request.email, secret);
    var lock = LockService.getScriptLock();
    if (!lock.tryLock(5000)) return studioEmailResponse_(false);
    try {
      // A request can become stale while waiting for the shared ScriptLock.
      if (!studioEmailFresh_(request.timestamp, now = Math.floor(Date.now() / 1000))) {
        return studioEmailResponse_(false);
      }

      var state = studioEmailReadState_(properties.getProperty(STUDIO_EMAIL_STATE_KEY_), now);
      if (!state) return studioEmailResponse_(false);
      var attempts = state.attempts;
      if (attempts.some(function (item) { return item.nonce === request.nonce; })) {
        return studioEmailResponse_(false);
      }
      if (attempts.length >= STUDIO_EMAIL_GLOBAL_LIMIT_) {
        return studioEmailResponse_(false);
      }
      var subjectCutoff = now - STUDIO_EMAIL_SUBJECT_WINDOW_;
      var subjectAttempts = attempts.filter(function (item) {
        return item.keyedSubjectHash === subject && item.acceptedAt > subjectCutoff;
      }).length;
      if (subjectAttempts >= STUDIO_EMAIL_SUBJECT_LIMIT_) {
        return studioEmailResponse_(false);
      }

      // MailApp reports a snapshot, not an atomic reservation.  Reconcile that
      // snapshot with the Studio sends which have been reserved but may not yet
      // be reflected in it.  This prevents overlapping Studio executions from
      // each spending the same monitoring headroom while keeping the shared
      // ScriptLock out of the provider call below.
      var remaining = MailApp.getRemainingDailyQuota();
      if (!studioEmailReserveQuota_(state, remaining, now)) {
        return studioEmailResponse_(false);
      }

      attempts.push({
        nonce: request.nonce,
        keyedSubjectHash: subject,
        acceptedAt: now
      });
      var encoded = JSON.stringify(state);
      if (encoded.length > 8500) return studioEmailResponse_(false);
      // Reserve before delivery.  A provider error after this write is
      // ambiguous and must never make the same signed request send twice.
      properties.setProperty(STUDIO_EMAIL_STATE_KEY_, encoded);
    } finally {
      lock.releaseLock();
    }

    // The shared lock is never held across the provider call.
    MailApp.sendEmail(
      request.email,
      'Your SFDC24 Studio verification code',
      'Your SFDC24 Studio verification code is ' + request.code +
          '. It expires in 10 minutes. If you did not request it, ignore this email.'
    );
    return studioEmailResponse_(true);
  } catch (ignored) {
    // Apps Script exceptions can contain request data.  Never log or return it.
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

function studioEmailFresh_(timestamp, now) {
  return Math.abs(now - Number(timestamp)) <= 120;
}

function studioEmailHmacHex_(message, secret) {
  return Utilities.computeHmacSha256Signature(
    message, secret, Utilities.Charset.UTF_8
  ).map(function (byte) {
    return ('0' + (byte & 255).toString(16)).slice(-2);
  }).join('');
}

function studioEmailEqual_(left, right) {
  // Both values are fixed-length hex strings by this point.  Compare all bytes.
  var difference = left.length ^ right.length;
  for (var i = 0; i < 64; i++) {
    difference |= left.charCodeAt(i) ^ right.charCodeAt(i);
  }
  return difference === 0;
}

function studioEmailReadState_(raw, now) {
  if (raw === null || raw === '') {
    return {
      version: 1,
      attempts: [],
      quotaFence: { observedRemaining: null, pending: [] }
    };
  }
  if (typeof raw !== 'string' || raw.length > 8500) return null;
  var parsed;
  try { parsed = JSON.parse(raw); }
  catch (ignored) { return null; }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed) ||
      Object.keys(parsed).sort().join(',') !== 'attempts,quotaFence,version' ||
      parsed.version !== 1 || !Array.isArray(parsed.attempts) ||
      parsed.attempts.length > STUDIO_EMAIL_GLOBAL_LIMIT_) {
    return null;
  }
  var fence = parsed.quotaFence;
  if (!fence || typeof fence !== 'object' || Array.isArray(fence) ||
      Object.keys(fence).sort().join(',') !== 'observedRemaining,pending' ||
      !(fence.observedRemaining === null ||
        (typeof fence.observedRemaining === 'number' &&
         isFinite(fence.observedRemaining) &&
         Math.floor(fence.observedRemaining) === fence.observedRemaining &&
         fence.observedRemaining >= 0 && fence.observedRemaining <= 100000)) ||
      !Array.isArray(fence.pending) ||
      fence.pending.length > STUDIO_EMAIL_GLOBAL_LIMIT_) {
    return null;
  }
  var cutoff = now - STUDIO_EMAIL_GLOBAL_WINDOW_;
  var attempts = [];
  for (var i = 0; i < parsed.attempts.length; i++) {
    var item = parsed.attempts[i];
    if (!item || typeof item !== 'object' || Array.isArray(item) ||
        Object.keys(item).sort().join(',') !== 'acceptedAt,keyedSubjectHash,nonce' ||
        !/^[0-9a-f]{32}$/.test(item.nonce) ||
        !/^[0-9a-f]{64}$/.test(item.keyedSubjectHash) ||
        typeof item.acceptedAt !== 'number' || !isFinite(item.acceptedAt) ||
        Math.floor(item.acceptedAt) !== item.acceptedAt || item.acceptedAt < 0 ||
        item.acceptedAt > now + 120) {
      return null;
    }
    if (item.acceptedAt > cutoff) attempts.push(item);
  }
  var pending = [];
  for (var j = 0; j < fence.pending.length; j++) {
    var reservedAt = fence.pending[j];
    if (typeof reservedAt !== 'number' || !isFinite(reservedAt) ||
        Math.floor(reservedAt) !== reservedAt || reservedAt < 0 ||
        reservedAt > now + 120) {
      return null;
    }
    if (reservedAt > cutoff) pending.push(reservedAt);
  }
  if (fence.observedRemaining === null && pending.length) return null;
  return {
    version: 1,
    attempts: attempts,
    quotaFence: {
      observedRemaining: fence.observedRemaining,
      pending: pending
    }
  };
}

function studioEmailReserveQuota_(state, remaining, now) {
  if (typeof remaining !== 'number' || !isFinite(remaining) ||
      Math.floor(remaining) !== remaining || remaining < 0 || remaining > 100000) {
    return false;
  }
  var fence = state.quotaFence;
  var observed = fence.observedRemaining;
  if (observed === null || remaining > observed) {
    // An increased quota indicates a provider reset or a higher current limit.
    fence.pending = [];
  } else if (remaining < observed && fence.pending.length) {
    // Provider consumption now reflects at least this many prior reservations.
    var reflected = Math.min(observed - remaining, fence.pending.length);
    fence.pending.splice(0, reflected);
  }
  fence.observedRemaining = remaining;
  if (remaining - fence.pending.length <= STUDIO_EMAIL_MONITOR_HEADROOM_) {
    return false;
  }
  fence.pending.push(now);
  return true;
}

function studioEmailResponse_(ok) {
  return ContentService.createTextOutput(JSON.stringify({ ok: ok }))
      .setMimeType(ContentService.MimeType.JSON);
}
