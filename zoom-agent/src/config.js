import 'dotenv/config';

function required(name) {
  const value = process.env[name];
  if (!value || value.startsWith('paste_')) {
    console.error(`\n✖ Missing env var ${name} — copy .env.example to .env and fill it in.\n`);
    process.exit(1);
  }
  return value;
}

// Optional: the agent must still start and listen when a downstream rail is not
// configured yet. A missing WhatsApp token degrades to console output; it does
// not stop the meeting listener.
function optional(name, fallback = '') {
  const value = process.env[name];
  if (!value || value.startsWith('paste_')) return fallback;
  return value;
}

const num = (name, fallback) => {
  const raw = process.env[name];
  const n = raw === undefined ? NaN : Number(raw);
  return Number.isFinite(n) ? n : fallback;
};

export const config = {
  // ── Zoom (required: without these there is nothing to listen to) ──
  clientId: required('ZOOM_CLIENT_ID'),
  clientSecret: required('ZOOM_CLIENT_SECRET'),
  wsEndpoint: required('ZOOM_WS_ENDPOINT'),
  redirectUri: process.env.ZOOM_REDIRECT_URI ?? 'http://localhost:3000/oauth/callback',
  port: num('PORT', 3000),
  tokenFile: process.env.TOKEN_FILE ?? '.tokens.json',

  // ── SFDC24 brain: the same reception() the website and WhatsApp call ──
  receptionExec: optional('SFDC24_EXEC'),

  // ── Delivery to the consultant's phone ──
  metaToken: optional('META_TOKEN'),
  waPhoneNumberId: optional('WA_PHONE_NUMBER_ID'),
  waTo: optional('WA_TO'),

  // ── The blackboard, for the post-call finding ──
  busUrl: optional('BUS_URL'),
  busSecret: optional('BUS_SECRET'),

  // ── Behaviour ──
  // Wake words are how it stays quiet by default. Say one out loud and it answers.
  wakeWords: optional('WAKE_WORDS', 'sfdc24,hey consultant')
    .split(',').map((w) => w.trim().toLowerCase()).filter(Boolean),
  // 0 = never volunteer. Any positive N = consider speaking every N lines, and
  // it may still decide there is nothing worth interrupting for.
  assistEveryN: num('ASSIST_EVERY_N', 0),
  contextLines: num('CONTEXT_LINES', 30),
  summaryLines: num('SUMMARY_LINES', 400),
  // Client conversations. Off by default so a transcript is not sprayed into a
  // terminal log by accident.
  logTranscript: optional('LOG_TRANSCRIPT', 'false') === 'true',
};

// The @zoom/rtms SDK reads these two names for the media-plane HMAC signature.
// Mirror them so a single .env drives both planes.
process.env.ZM_RTMS_CLIENT ??= config.clientId;
process.env.ZM_RTMS_SECRET ??= config.clientSecret;
