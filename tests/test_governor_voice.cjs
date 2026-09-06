const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const CODE_PATH = path.join(ROOT, "apps-script", "governor-page-api", "Code.gs");
const RECEPTION_PATH = path.join(ROOT, "apps-script", "governor-page-api", "Reception.html");
const CODE = readFileSync(CODE_PATH, "utf8");

class ScriptProperties {
  constructor(initial = {}) { this.values = new Map(Object.entries(initial)); }
  getProperty(name) { return this.values.has(name) ? this.values.get(name) : null; }
  setProperty(name, value) { this.values.set(name, String(value)); return this; }
  deleteProperty(name) { this.values.delete(name); return this; }
  getProperties() { return Object.fromEntries(this.values); }
}

class ScriptCache {
  constructor() { this.values = new Map(); }
  get(name) { return this.values.has(name) ? this.values.get(name) : null; }
  put(name, value) { this.values.set(name, String(value)); }
  remove(name) { this.values.delete(name); }
}

class ScriptLock {
  constructor() { this.held = false; }
  waitLock() {
    if (this.held) throw new Error("mock lock already held");
    this.held = true;
  }
  releaseLock() { this.held = false; }
}

function response(code, text, bytes = Buffer.from(text)) {
  return {
    getResponseCode: () => code,
    getContentText: () => text,
    getContent: () => bytes
  };
}

function createHarness(initialProperties = {}) {
  const harness = {
    properties: new ScriptProperties(initialProperties),
    cache: new ScriptCache(),
    lock: new ScriptLock(),
    chatCalls: 0,
    ttsProviderCalls: 0,
    chatDailyUsed: 0,
    failChat: false,
    uuid: 0,
    onTtsFetch: null,
    logs: []
  };

  const context = {
    Buffer,
    CacheService: { getScriptCache: () => harness.cache },
    ContentService: {
      MimeType: { JSON: "application/json", JAVASCRIPT: "text/javascript" },
      createTextOutput(text) {
        return { text, setMimeType() { return this; } };
      }
    },
    LockService: { getScriptLock: () => harness.lock },
    Logger: { log: (message) => harness.logs.push(String(message)) },
    PropertiesService: { getScriptProperties: () => harness.properties },
    Utilities: {
      Charset: { UTF_8: "UTF_8" },
      DigestAlgorithm: { SHA_256: "SHA_256" },
      base64Encode: (bytes) => Buffer.from(bytes).toString("base64"),
      computeDigest: (_algorithm, value) => Array.from(createHash("sha256").update(String(value)).digest())
        .map((byte) => byte > 127 ? byte - 256 : byte),
      formatDate: (date) => new Date(date).toISOString().slice(0, 10),
      getUuid: () => (++harness.uuid).toString(16).padStart(16, "0") + "0000000000000000"
    },
    UrlFetchApp: {
      fetch(url) {
        if (url.includes("anthropic.com")) {
          harness.chatCalls += 1;
          if (harness.failChat) throw new Error("mock upstream failure");
          return response(200, JSON.stringify({ content: [{ type: "text", text: "A useful healthy reply." }] }));
        }
        if (url.includes("api.openai.com/v1/audio/speech")) {
          harness.ttsProviderCalls += 1;
          if (harness.onTtsFetch) harness.onTtsFetch();
          return response(200, "", Buffer.from("mock-mp3"));
        }
        throw new Error("unexpected fetch " + url);
      }
    }
  };

  vm.createContext(context);
  vm.runInContext(CODE, context, { filename: CODE_PATH });
  context.readSession_ = () => null;
  context.logVisitor_ = () => {};
  context.dailyCount_ = () => harness.chatDailyUsed;
  context.bumpDaily_ = () => { harness.chatDailyUsed += 1; };
  harness.context = context;
  return harness;
}

function decode(output) {
  return JSON.parse(output.text);
}

function healthyHarness(extra = {}) {
  return createHarness({
    ANTHROPIC_KEY: "configured-for-mock",
    OPENAI_KEY: "configured-for-mock",
    ...extra
  });
}

function issueAudioKey(harness, sid = "session-one") {
  const result = decode(harness.context.voiceReply_({ vid: sid, q: "Help with a Salesforce flow" }));
  assert.equal(result.ok, true);
  assert.match(result.ak, /^ak[a-f0-9]{16}$/);
  return result.ak;
}

test("degraded chat branches never mint a paid-audio key", () => {
  const cases = [
    {
      name: "chat off",
      properties: { ANTHROPIC_KEY: "mock", OPENAI_KEY: "mock", CHAT_ENABLED: "off" }
    },
    {
      name: "missing chat key",
      properties: { OPENAI_KEY: "mock" }
    },
    {
      name: "session cap",
      properties: { ANTHROPIC_KEY: "mock", OPENAI_KEY: "mock" },
      prepare: (h) => h.cache.put("rc_session-one", "12")
    },
    {
      name: "daily cap",
      properties: { ANTHROPIC_KEY: "mock", OPENAI_KEY: "mock", CHAT_DAILY_CAP: "1" },
      prepare: (h) => { h.chatDailyUsed = 1; }
    },
    {
      name: "chat API failure",
      properties: { ANTHROPIC_KEY: "mock", OPENAI_KEY: "mock" },
      prepare: (h) => { h.failChat = true; }
    }
  ];

  for (const item of cases) {
    const h = createHarness(item.properties);
    if (item.prepare) item.prepare(h);
    const result = decode(h.context.voiceReply_({ vid: "session-one", q: "hello" }));
    assert.equal(result.ok, true, item.name);
    assert.ok(result.degraded, item.name);
    assert.equal(Object.hasOwn(result, "ak"), false, item.name);
    assert.equal(h.ttsProviderCalls, 0, item.name);
  }
});

test("a healthy reply mints a short-lived server-side claim without calling TTS", () => {
  const h = healthyHarness();
  const ak = issueAudioKey(h);
  assert.ok(h.properties.getProperty(h.context.TTS_KEY_PROP_PREFIX + ak));
  assert.equal(h.cache.get("tts_" + ak), "A useful healthy reply.");
  assert.equal(h.ttsProviderCalls, 0);
});

test("whole-site TTS budget exhaustion fails closed before provider fetch", () => {
  const h = healthyHarness();
  const ak = issueAudioKey(h);
  h.properties.setProperty("TTS_DAILY_CAP", "0");

  const result = decode(h.context.ttsAudio_({ ak }));
  assert.deepEqual(result, { ok: false, reason: "tts-daily-cap" });
  assert.equal(h.ttsProviderCalls, 0);
});

test("per-session TTS budget is reserved atomically and fails closed", () => {
  const h = healthyHarness({ TTS_SESSION_CAP: "1" });
  const first = issueAudioKey(h, "one-session");
  const second = issueAudioKey(h, "one-session");

  assert.equal(decode(h.context.ttsAudio_({ ak: first })).ok, true);
  assert.deepEqual(decode(h.context.ttsAudio_({ ak: second })), {
    ok: false,
    reason: "tts-session-cap"
  });
  assert.equal(h.ttsProviderCalls, 1);
});

test("reusing one audio key reaches the provider only once", () => {
  const h = healthyHarness();
  const ak = issueAudioKey(h);

  assert.equal(decode(h.context.ttsAudio_({ ak })).ok, true);
  assert.deepEqual(decode(h.context.ttsAudio_({ ak })), { ok: false, reason: "expired" });
  assert.equal(h.ttsProviderCalls, 1);
  assert.equal(h.logs.length, 1);
  assert.match(h.logs[0], /^TTS provider attempt: daily \d+\/\d+, session \d+\/\d+$/);
  assert.equal(h.logs[0].includes(ak), false);
  assert.equal(h.logs[0].includes("A useful healthy reply"), false);
});

test("a simultaneous second consumer cannot pass the deleted claim", () => {
  const h = healthyHarness();
  const ak = issueAudioKey(h);
  let racedResult;
  h.onTtsFetch = () => {
    racedResult = decode(h.context.ttsAudio_({ ak }));
  };

  const first = decode(h.context.ttsAudio_({ ak }));
  assert.equal(first.ok, true);
  assert.deepEqual(racedResult, { ok: false, reason: "expired" });
  assert.equal(h.ttsProviderCalls, 1);
});

test("reception emits the nonce-bound application-ready message", () => {
  const reception = readFileSync(RECEPTION_PATH, "utf8");
  assert.match(CODE, /readyNonce\s*=\s*\/\^\[A-Za-z0-9_-/);
  assert.match(reception, /type:\s*"sfdc24:assistant-ready"/);
  assert.match(reception, /nonce:\s*READY_NONCE/);
  assert.doesNotMatch(reception, /postMessage\([^)]*(visitorEmail|sessionToken|SESSION)/);
});
