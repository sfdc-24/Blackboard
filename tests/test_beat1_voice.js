const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

// Run the shipped local controller against deferred fetch and fake device APIs.
// No provider, microphone, audio device or customer environment is contacted.
const html = fs.readFileSync(path.join(__dirname, '../prototype/beat1/voice.html'), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi)];
const controller = scripts.filter(script => script[1].trim());
assert.equal(controller.length, 1, 'exercise the one shipped inline voice controller');

function harness({ recognitionAvailable = true, speechAvailable = true } = {}) {
  function element() {
    return {
      children: [], listeners: {}, className: '', value: '', textContent: '', style: {},
      addEventListener(name, fn) { this.listeners[name] = fn; },
      setAttribute(name, value) { this[name] = value; },
      appendChild(child) { child.parentNode = this; this.children.push(child); },
      removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; },
      get lastElementChild() { return this.children.at(-1); },
      get firstChild() { return this.children[0]; },
      querySelector(selector) { return this.children.find(child => '.' + child.className === selector); },
      querySelectorAll(selector) {
        const classes = selector.split('.').filter(Boolean);
        return this.children.filter(child => classes.every(name => child.className.split(/\s+/).includes(name)));
      },
      focus() {},
    };
  }
  const elements = Object.fromEntries([
    'app', 'tape', 'mic', 'warn', 'box', 'send', 'hint', 'bars', 'stage', 'sk', 'stitle',
    'state', 'stateword', 'statewho', 'statevoice', 'voices', 'voicebtn',
  ].map(id => [id, element()]));
  const recognition = [];
  let starts = 0, aborts = 0;
  class Recognition {
    constructor() { recognition.push(this); }
    start() { starts++; }
    abort() { aborts++; if (this.onend) this.onend(); }
  }
  const spoken = [];
  const speechSynthesis = {
    getVoices: () => [], cancel() {}, speak: utterance => spoken.push(utterance),
  };
  class Audio {
    play() { return { catch() {} }; }
    pause() {}
  }
  const window = {
    SpeechRecognition: recognitionAvailable ? Recognition : undefined,
    speechSynthesis: speechAvailable ? speechSynthesis : undefined,
  };
  const requests = [];
  const settle = () => new Promise(resolve => setImmediate(resolve));
  vm.runInNewContext(controller[0][1], {
    window, document: { getElementById: id => elements[id], createElement: element, createElementNS: element },
    Audio, SpeechSynthesisUtterance: function(text) { this.text = text; },
    localStorage: { getItem: () => null, setItem() {} },
    navigator: { userAgent: 'synthetic test' },
    Beat1History: require('../prototype/beat1/history.js'),
    fetch(route, options) {
      return new Promise((resolve, reject) => requests.push({
        route, options, reject,
        reply(value) { resolve({ json: () => Promise.resolve(value) }); },
      }));
    },
  });
  return {
    get starts() { return starts; },
    get aborts() { return aborts; },
    get mode() { return elements.stateword.textContent; },
    get currentRecognition() { return recognition.at(-1); },
    get spokenCount() { return spoken.filter(utterance => utterance.text.trim()).length; },
    get turnCount() { return elements.tape.children.length; },
    get liveCount() { return elements.tape.children.filter(node => node.className === 'turn live').length; },
    get draft() { return elements.box.value; },
    get sendDisabled() { return elements.send.disabled; },
    tapMic() { elements.mic.listeners.click(); },
    edit(text) { elements.box.value = text; elements.box.listeners.input(); },
    type(text) {
      elements.box.value = text;
      elements.box.listeners.input();
      elements.send.listeners.click();
    },
    recognize(text) {
      const result = [{ transcript: text }]; result.isFinal = true;
      recognition.at(-1).onresult({ resultIndex: 0, results: [result] });
      recognition.at(-1).onend();
    },
    interim(text) {
      const result = [{ transcript: text }]; result.isFinal = false;
      recognition.at(-1).onresult({ resultIndex: 0, results: [result] });
    },
    replayFirst() {
      const reply = elements.tape.children.find(node => node.children.some(child => child.className === 'replay'));
      reply.children.find(child => child.className === 'replay').listeners.click();
      return spoken.at(-1).onend;
    },
    async answer(text = 'A synthetic answer.') {
      const count = spoken.length;
      assert.ok(requests.length, 'expected a pending turn');
      requests.shift().reply({ ok: true, reply: text });
      await settle();
      return spoken.length > count ? spoken.at(-1).onend : undefined;
    },
    async reject() { requests.shift().reject(new Error('synthetic network error')); await settle(); },
  };
}

test('typing never opts into microphone capture', async () => {
  const h = harness();
  h.type('Explain this service.');
  assert.equal(h.starts, 0);
  const finish = await h.answer();
  assert.equal(h.mode, 'speaking');
  finish();
  assert.equal(h.mode, 'ready');
  assert.equal(h.starts, 0);
});

test('explicit Start talking preserves continuous recognition and silence retry', async () => {
  const h = harness();
  h.tapMic();
  h.currentRecognition.onend();
  assert.equal(h.starts, 2);
  h.recognize('A spoken question.');
  (await h.answer())();
  assert.equal(h.mode, 'listening');
  assert.equal(h.starts, 3);
});

test('Stop during pending reply prevents later speech and microphone restart', async () => {
  const h = harness();
  h.tapMic(); h.recognize('Question.'); h.tapMic();
  assert.equal(await h.answer(), undefined);
  assert.equal(h.spokenCount, 0);
  assert.equal(h.starts, 1);
  assert.equal(h.mode, 'ready');
});

test('Stop during speech invalidates late completion and subsequent chunks', async () => {
  const h = harness();
  h.tapMic(); h.recognize('Question.');
  const finish = await h.answer('Long synthetic sentence '.repeat(10) + '. Another sentence.');
  h.tapMic(); finish();
  assert.equal(h.spokenCount, 1);
  assert.equal(h.starts, 1);
  assert.equal(h.mode, 'ready');
});

test('typing interrupts listening even when abort synchronously fires onend', async () => {
  const h = harness();
  h.tapMic(); h.type('I will type now.');
  (await h.answer())();
  assert.equal(h.starts, 1);
  assert.equal(h.mode, 'ready');
});

test('old completion cannot finish a newer typed exchange', async () => {
  const h = harness();
  h.type('First.'); const oldFinish = await h.answer();
  h.type('Second.'); const newFinish = await h.answer();
  oldFinish(); assert.equal(h.mode, 'speaking');
  newFinish(); assert.equal(h.mode, 'ready');
  assert.equal(h.starts, 0);
});

test('stale recognizer events cannot restart a newly requested session', () => {
  const h = harness();
  h.tapMic(); const oldRecognition = h.currentRecognition;
  h.tapMic(); h.tapMic(); oldRecognition.onend();
  assert.equal(h.starts, 2);
  assert.equal(h.mode, 'listening');
});

test('replaying an old answer cancels a pending newer exchange without microphone intent', async () => {
  const h = harness();
  h.type('First.'); (await h.answer())();
  h.type('Second.'); const replayFinish = h.replayFirst();
  assert.equal(await h.answer(), undefined);
  assert.equal(h.mode, 'speaking');
  replayFinish();
  assert.equal(h.mode, 'ready');
  assert.equal(h.starts, 0);
});

test('late request failure cannot interrupt a newer listening session', async () => {
  const h = harness();
  h.type('First.'); h.tapMic(); h.tapMic();
  const turns = h.turnCount;
  await h.reject();
  assert.equal(h.mode, 'listening');
  assert.equal(h.turnCount, turns);
  assert.equal(h.starts, 1);
});

test('typed fallback works without browser recognition or speech synthesis', async () => {
  const h = harness({ recognitionAvailable: false, speechAvailable: false });
  h.type('Typed fallback.'); await h.answer();
  assert.equal(h.starts, 0);
  assert.equal(h.mode, 'ready');
});

test('editing before submit stops recognition and removes its interim transcript', () => {
  const h = harness();
  h.tapMic(); h.interim('unfinished spoken words');
  assert.equal(h.liveCount, 1);
  h.edit('A keyboard draft');
  assert.equal(h.mode, 'ready');
  assert.equal(h.draft, 'A keyboard draft');
  assert.equal(h.liveCount, 0);
  assert.equal(h.aborts, 1, 'drafting must abort capture, not only change the UI');
  h.currentRecognition.onend();
  assert.equal(h.starts, 1);
});

test('editing during a pending spoken reply preserves the reply but cancels automatic listening', async () => {
  const h = harness();
  h.tapMic(); h.recognize('Question.');
  h.edit('My next typed question');
  assert.equal(h.mode, 'thinking');
  assert.equal(h.sendDisabled, true);
  (await h.answer())();
  assert.equal(h.spokenCount, 1);
  assert.equal(h.mode, 'ready');
  assert.equal(h.starts, 1);
  assert.equal(h.draft, 'My next typed question');
  assert.equal(h.sendDisabled, false);
});

test('Stop clears interim text without letting an old end event clear a new preview', () => {
  const h = harness();
  h.tapMic(); h.interim('Old preview');
  const oldRecognition = h.currentRecognition;
  h.tapMic();
  assert.equal(h.liveCount, 0);
  assert.equal(h.aborts, 1);
  h.tapMic(); h.interim('Current preview');
  oldRecognition.onend();
  assert.equal(h.liveCount, 1);
  assert.equal(h.mode, 'listening');
  assert.equal(h.starts, 2);
});

test('editing while speech plays keeps the current answer but disables its microphone continuation', async () => {
  const h = harness();
  h.tapMic(); h.recognize('Question.');
  const finish = await h.answer();
  h.edit('Next typed question');
  assert.equal(h.mode, 'speaking');
  finish();
  assert.equal(h.mode, 'ready');
  assert.equal(h.starts, 1);
});
