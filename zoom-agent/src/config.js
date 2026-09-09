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
  // The OAuth callback listens on LOOPBACK ONLY unless an operator overrides it
  // by hand. Express's default bound every interface — `server.address()` on the
  // reviewed head returned {address:'::'} — which published the authorization
  // endpoint of a process holding a long-lived Zoom refresh token to whatever
  // network the host was on. Zoom's own guidance is a loopback literal.
  oauthHost: optional('OAUTH_HOST', '127.0.0.1'),
  tokenFile: process.env.TOKEN_FILE ?? '.tokens.json',

  // ── SFDC24 brain: the same reception() the website and WhatsApp call ──
  receptionExec: optional('SFDC24_EXEC'),

  // ── Delivery to the consultant's phone ──
  metaToken: optional('META_TOKEN'),
  waPhoneNumberId: optional('WA_PHONE_NUMBER_ID'),
  waTo: optional('WA_TO'),
  // Absolute ceiling on the Graph call. A hung send used to hold the meeting's
  // inFlight lock forever, which silences the assistant for the rest of the
  // call and looks exactly like a quiet meeting.
  notifyTimeoutMs: num('NOTIFY_TIMEOUT_MS', 15_000),

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
  // How many 1,000-char segments a wrap-up may spend. reception() cuts `q` at
  // CHAT_MAX_INPUT, so a long call has to be summarised in pieces — and every
  // piece is a paid backend call against the shared caps. Eight segments covers
  // roughly the last 6,000 characters of speech; beyond that the summary says
  // how many lines it did not reach rather than pretending to cover them.
  summaryMaxChunks: num('SUMMARY_MAX_CHUNKS', 8),
  // POST-CALL WRAP-UP: OFF until the backend has a trusted summarisation route.
  //
  // reception() has one persona -- the sfdc24.com visitor receptionist -- and it
  // is instructed never to obey instructions embedded in a message and never to
  // answer with a list (Code.gs:614,623). A wrap-up is exactly that: an embedded
  // command asking for a three-part list. The persona may legitimately decline
  // and answer conversationally, and nothing on this side can tell the
  // difference between that and a real summary.
  //
  // Held on the PM decision of 2026-09-09 (PR #40). Live wake-word assistance is
  // unaffected and remains on. Turn this on only once action=say is no longer
  // the route -- or knowingly, for a test.
  wrapUpEnabled: optional('WRAP_UP_ENABLED', 'false') === 'true',
  // Client conversations. Off by default so a transcript is not sprayed into a
  // terminal log by accident.
  logTranscript: optional('LOG_TRANSCRIPT', 'false') === 'true',
};

// The @zoom/rtms SDK reads these two names for the media-plane HMAC signature.
// Mirror them so a single .env drives both planes.
process.env.ZM_RTMS_CLIENT ??= config.clientId;
process.env.ZM_RTMS_SECRET ??= config.clientSecret;
