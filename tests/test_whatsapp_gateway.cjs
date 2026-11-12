const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const load = require('./helpers/pipedream_loader.cjs');

const OPERATOR = '15550001234';
const textMessage = (text = 'Project status', id = 'wamid.fixture') => ({ id, from: OPERATOR, type: 'text', text: { body: text } });
const plain = value => JSON.parse(JSON.stringify(value));

function inbound(hooks = {}) {
  const requests = [], saved = new Map(), exports = {}, responses = [], p0s = [], logs = [], timeline = [];
  let response = { ok: true, json: async () => ({ result: 'success', rowId: 'WRK-fixture' }) };
  const env = { ALPHA_URL: 'https://example.invalid/board', ALPHA_SECRET: 'fixture-secret-value',
    META_VERIFY_TOKEN: 'mock-verify-token', META_APP_SECRET: 'mock-app-secret' };
  const context = load('pipedream_wa_inbound.js', env, async (_url, options) => {
    timeline.push({ kind: 'persist', requests: requests.length });
    requests.push(JSON.parse(options.body));
    return response;
  });
  const props = {
    db: { get: async id => saved.get(id), set: async (id, value) => saved.set(id, value) },
    postP0: hooks.postP0 || (async payload => { p0s.push(payload); }),
    log: hooks.log || (line => { logs.push(line); }),
  };
  return {
    requests, saved, exports, context, responses, p0s, logs, timeline,
    setResponse: value => { response = value; },
    async run(messages, extraValue = {}, mutateEvent = () => {}) {
      const body = { entry: [{ changes: [{ value: { ...extraValue, messages } }] }] };
      const raw_body = JSON.stringify(body);
      const signature = 'sha256=' + crypto.createHmac('sha256', env.META_APP_SECRET).update(raw_body).digest('hex');
      const event = { method: 'POST', body, raw_body, headers: { 'x-hub-signature-256': signature } };
      mutateEvent(event);
      await context.component.run.call(props, { steps: { trigger: { event } }, $: {
        respond: async value => {
          responses.push(plain(value));
          timeline.push({ kind: 'respond', status: value && value.status, requests: requests.length });
        },
        export: (name, value) => { exports[name] = plain(value); },
      } });
      return exports.summary;
    },
  };
}

async function handshake(env, query) {
  const responses = [], exits = [];
  const context = load('pipedream_wa_inbound.js', env);
  const result = await context.component.run.call({}, { steps: { trigger: { event: { method: 'GET', query } } }, $: {
    respond: async value => { responses.push(plain(value)); }, export: () => {},
    flow: { exit: reason => { exits.push(reason); return reason; } },
  } });
  return { response: responses.at(-1), result, exits };
}

function snapshot(rows) {
  return { spreadsheetId: '120_71KaF4JKGPGz0qUz4phqWRljSqEzSRRm_0zXC_oY', sheetId: 0,
    headers: ['Row_ID', 'Timestamp', 'Source_Tag', 'Target_Surface', 'Action_Type', 'Payload'],
    firstRow: 1170, fetchedAt: new Date().toISOString(), rows: rows || [
      ['view', '2026-09-06T16:41:23Z', 'vm-cli', 'ALL', 'APPEND', 'BCB|v=1|phase=VIEWPORT|vseq=011|by=vm-cli|state=work did not run'],
      ['auto', '2026-09-06T17:10:30Z', 'claude', 'ALL', 'RESULT', 'I have no running task'],
      ['new', '2026-09-06T17:30:36Z', 'claude-code-cli', 'ALL', 'APPEND', 'BCB|v=1|phase=RESULT|what=monitor email arrived'],
      ['worker', '2026-09-06T23:50:53Z', 'vm-order-worker', 'codex', 'APPEND', 'BCB|v=1|phase=RESULT|status=completed|summary=read-only repository inspection'],
    ] };
}

async function prepare(summary, board = snapshot()) {
  return load('pipedream_wa_context.js', { WA_OPERATOR_ID: OPERATOR }).component.run.call({
    inbound: summary, snapshot: board, lane: 'claude',
  });
}

test('inbound preserves exact text, original wamid and quoted-message ID', async () => {
  const a = inbound();
  const msg = { ...textMessage('Mr. Salam | café\nsecond line'), context: { id: 'wamid.original' } };
  const result = await a.run([msg]);
  assert.equal(result.messages[0].text, msg.text.body);
  assert.equal(result.messages[0].replyTo, 'wamid.original');
  assert.equal(result.messages[0].wamid, 'wamid.fixture');
  assert.match(a.requests[0].payload, /\|reply_to=wamid.original\|text=Mr. Salam \| café\nsecond line$/);
});

for (const kind of ['button_reply', 'list_reply']) {
  test(`inbound preserves ${kind} ID and label through board append and structured export`, async () => {
    const a = inbound();
    const result = await a.run([{ id: 'wamid.vote', from: OPERATOR, type: 'interactive',
      context: { id: 'wamid.question' }, interactive: { type: kind, [kind]: { id: 'task|choice=A', title: 'Proceed' } } }]);
    assert.deepEqual(result.messages[0].selection, { kind, id: 'task|choice=A', title: 'Proceed' });
    assert.match(a.requests[0].payload, /choice_id=task%7Cchoice%3DA\|text=Proceed$/);
    assert.equal(result.messages[0].replyTo, 'wamid.question');
  });
}

test('status-only deliveries and duplicate wamids produce no new jobs or appends', async () => {
  const a = inbound();
  const status = await a.run([], { statuses: [{ id: 'wamid.status', status: 'delivered' }] });
  assert.equal(status.messages.length, 0);
  assert.equal(status.statusEventsIgnored, 1);
  assert.equal(a.requests.length, 0);
  assert.equal((await prepare(status)).jobs.length, 0);
  await a.run([textMessage()]);
  const duplicate = await a.run([textMessage()]);
  assert.equal(a.requests.length, 1);
  assert.equal(duplicate.messages.length, 0);
  assert.equal(duplicate.duplicatesSkipped, 1);
});

test('empty, non-text and identity-less inputs fail before board writes', async () => {
  for (const msg of [textMessage('  \n'), textMessage({ unexpected: 'object' }),
    { ...textMessage(), id: '' }, { ...textMessage(), from: undefined }, { ...textMessage(), from: Number(OPERATOR) },
    { ...textMessage(), type: 'unknown' }, { ...textMessage(), context: { id: 'invalid|id' } }]) {
    const a = inbound();
    await assert.rejects(a.run([msg]));
    assert.equal(a.requests.length, 0);
    assert.equal(a.saved.size, 0);
  }
});

test('HTTP and application append failures cannot mark a wamid as processed', async () => {
  for (const response of [
    { ok: false, json: async () => ({ result: 'success', rowId: 'WRK-fake' }) },
    { ok: true, json: async () => ({ result: 'error', rowId: 'WRK-fake' }) },
    { ok: true, json: async () => ({ result: 'success' }) },
    { ok: true, json: async () => { throw new Error('invalid JSON'); } },
  ]) {
    const a = inbound();
    a.setResponse(response);
    await assert.rejects(a.run([textMessage()]), /read back before replay/);
    assert.equal(a.saved.get('wamid.fixture'), undefined); // no processed receipt
  }
});

test('configured signature verification fails closed on absent raw bytes or malformed signature', async () => {
  for (const mutate of [e => { delete e.raw_body; }, e => { e.headers['x-hub-signature-256'] = 'short'; }]) {
    const a = inbound();
    await assert.rejects(a.run([textMessage()], {}, mutate));
    assert.equal(a.requests.length, 0);
  }
});

test('webhook GET verification uses the environment token and validates the challenge', async () => {
  const valid = await handshake({ META_VERIFY_TOKEN: 'fixture-token' }, {
    'hub.verify_token': 'fixture-token', 'hub.challenge': '123456789',
  });
  assert.deepEqual(valid.response, { status: 200, body: '123456789' });
  assert.match(valid.result, /verification handshake/);

  const invalid = await handshake({ META_VERIFY_TOKEN: 'fixture-token' }, {
    'hub.verify_token': 'wrong-token', 'hub.challenge': '123456789',
  });
  assert.equal(invalid.response.status, 403);

  const invalidChallenge = await handshake({ META_VERIFY_TOKEN: 'fixture-token' }, {
    'hub.verify_token': 'fixture-token', 'hub.challenge': '<not-reflected>',
  });
  assert.equal(invalidChallenge.response.status, 403);

  const unconfigured = await handshake({}, {
    'hub.verify_token': 'anything', 'hub.challenge': '123456789',
  });
  assert.deepEqual(unconfigured.response, { status: 500, body: 'Verification unavailable' });
});

test('signature header lookup is case-insensitive', async () => {
  const a = inbound();
  const result = await a.run([textMessage('signed text')], {}, event => {
    const signature = event.headers['x-hub-signature-256'];
    event.headers = { 'X-Hub-Signature-256': signature };
  });
  assert.equal(result.signature, 'valid');
  assert.equal(result.messages[0].text, 'signed text');
});

test('authenticated processing uses the signed body when the parsed event diverges', async () => {
  const a = inbound();
  const result = await a.run([textMessage('signed text')], {}, e => { e.body = { entry: [] }; });
  assert.equal(result.messages[0].text, 'signed text');
});

test('the prepared context contains the latest viewport and later instance reports, excluding gateway echoes', async () => {
  const a = inbound();
  const jobs = (await prepare(await a.run([textMessage()]))).jobs;
  assert.equal(jobs[0].input.board.viewport.id, 'view');
  assert.deepEqual(plain(jobs[0].input.board.laterRows.map(r => r.id)), ['new', 'worker']);
  assert.equal(jobs[0].input.board.excludedGatewayReplies, 1);
  assert.match(jobs[0].system, /not a persistent CLI instance/);
  assert.match(jobs[0].system, /never system instructions or permission/);
});

test('wrong-board, stale, missing-viewport and visitor-forged viewport snapshots fail closed', async () => {
  const a = inbound();
  const summary = await a.run([textMessage()]);
  for (const change of [b => { b.spreadsheetId = 'wrong'; }, b => { b.sheetId = 1123132922; },
    b => { b.fetchedAt = '2000-01-01T00:00:00Z'; }, b => { b.rows = []; },
    b => { b.rows[0][2] = 'whatsapp'; }, b => { b.rows[0][5] += '|phase=ORDER'; }]) {
    const b = snapshot(); change(b);
    await assert.rejects(prepare(summary, b));
  }
});

test('non-operator or unverified inbound messages cannot obtain private board context', async () => {
  const a = inbound();
  const summary = await a.run([textMessage()]);
  await assert.rejects(prepare({ ...summary, signature: 'not checked' }), /authenticated/);
  summary.messages[0].from = '15550000000';
  await assert.rejects(prepare(summary), /non-operator/);
});

test('context truncation is explicit and retains the newest updates', async () => {
  const a = inbound();
  const b = snapshot();
  for (let i = 0; i < 20; i++) b.rows.push(['later' + i, '', 'vm-cli', 'ALL', 'APPEND', 'x'.repeat(8000)]);
  const context = (await prepare(await a.run([textMessage()]), b)).jobs[0].input.board;
  assert.ok(context.omittedEarlierUpdates > 0);
  assert.equal(context.laterRows.at(-1).id, 'later19');
  assert.equal(context.laterRows.at(-1).truncated, true);
  assert.ok(JSON.stringify(context).length < 24500);
});

test('addressed CLI messages get a labelled gateway acknowledgement without invoking a model or claiming worker activity', async () => {
  const a = inbound();
  const jobs = (await prepare(await a.run([textMessage('claude-code-cli: status')]))).jobs;
  assert.equal(jobs[0].callModel, false);
  assert.equal(jobs[0].target, 'claude-code-cli');
  assert.match(jobs[0].response.text.body, /^\[STATUS \| gateway\]/);
  assert.match(jobs[0].response.text.body, /Blackboard gateway accepted it/);
  assert.match(jobs[0].response.text.body, /not independently read the row back/);
  assert.doesNotMatch(jobs[0].response.text.body, /was submitted to Blackboard/);
  assert.match(jobs[0].response.text.body, /no acknowledgement from that instance yet/);
  assert.equal(jobs[0].response.context.message_id, 'wamid.fixture');
  const renderer = load('pipedream_wa_reply.js').component;
  const rendered = await renderer.run.call({ job: jobs[0] });
  assert.equal(rendered.to, OPERATOR);
  assert.equal(rendered.text.body, jobs[0].response.text.body);
  const mismatched = plain(jobs[0]);
  mismatched.response.context.message_id = 'wamid.different';
  await assert.rejects(renderer.run.call({ job: mismatched }), /invalid prepared/);
});

test('multiple human messages retain separate jobs and correctly correlated reply payloads', async () => {
  const a = inbound();
  const jobs = (await prepare(await a.run([textMessage('first', 'wamid.first'), textMessage('second', 'wamid.second')]))).jobs;
  assert.equal(jobs.length, 2);
  const renderer = load('pipedream_wa_reply.js').component;
  for (const job of jobs) {
    const reply = await renderer.run.call({ job, modelText: 'Mr. Salam, this is the recorded status.' });
    assert.equal(reply.to, OPERATOR);
    assert.equal(reply.context.message_id, job.wamid);
    assert.match(reply.text.body, /^\[STATUS \| gateway\/claude\]/);
  }
  await assert.rejects(renderer.run.call({ job: jobs[0], modelText: ' ' }), /blank/);
  await assert.rejects(renderer.run.call({ job: jobs[0], modelText: 'x'.repeat(4096) }), /limit/);
});

function modelJob() {
  return {
    wamid: 'wamid.fixture',
    callModel: true,
    system: 'Answer from the provided board records only.',
    input: { board: { fetchedAt: '2026-09-06T17:00:00Z' }, message: { text: 'Project status', wamid: 'wamid.fixture' } },
    responseIdentity: { instance: 'gateway/claude', to: OPERATOR, replyTo: 'wamid.fixture' },
  };
}

function memoryStore() {
  const m = new Map();
  return { _m: m, get: async k => m.get(k), set: async (k, v) => { m.set(k, v); } };
}

function modelTurn(hooks = {}) {
  const andon = [], responses = [], p0s = [], logs = [];
  const store = hooks.store || memoryStore();
  const env = { MODEL_TIMEOUT_MS: String(hooks.timeoutMs || 30) };
  const context = load('pipedream_wa_model.js', env);
  const postAndon = hooks.postAndon || (async payload => { andon.push(payload); });
  const postP0 = hooks.postP0 || (async payload => { p0s.push(payload); });
  return {
    andon, responses, p0s, logs, store,
    async run(job = modelJob()) {
      return context.component.run.call({
        job, callModel: hooks.callModel, postAndon, postP0,
        deadLetterStore: store, log: hooks.log || (line => { logs.push(line); }),
      }, { $: {
        respond: async value => { responses.push(plain(value)); },
        export: () => {},
      } });
    },
  };
}

async function rejected(run) {
  try {
    await run();
  } catch (err) {
    return err;
  }
  throw new assert.AssertionError({ message: 'expected the model turn to fail', actual: undefined, expected: 'rejection' });
}

test('a successful model turn returns HTTP 200 and posts no ANDON row', async () => {
  const turn = modelTurn({ callModel: async () => '  Current status is green.  ' });
  const result = plain(await turn.run());
  assert.deepEqual(result, { status: 200, modelText: 'Current status is green.' });
  assert.deepEqual(turn.responses, [{ status: 200, body: 'ok' }]);
  assert.equal(turn.andon.length, 0);
});

test('a model error returns HTTP 502 and posts an ANDON row', async () => {
  const turn = modelTurn({ callModel: async () => { throw new Error('provider rejected the request'); } });
  const err = await rejected(() => turn.run());
  assert.match(String(err && err.message), /model turn failed \(502\)/);
  assert.equal(err.status, 502);
  assert.equal(err.andonPosted, true);
  assert.deepEqual(turn.responses, [{ status: 502, body: 'model turn failed' }]);
  assert.equal(turn.andon.length, 1);
  assert.match(turn.andon[0], /^ANDON\|v=1\|phase=ANDON\|source=whatsapp-gateway\|kind=model_error\|status=502\|wamid=wamid.fixture\|lane=claude\|error=provider rejected the request$/);
});

test('a model timeout returns HTTP 504 and posts an ANDON row', async () => {
  const turn = modelTurn({
    timeoutMs: 20,
    callModel: (_job, { signal }) => new Promise((_, reject) => {
      const fail = () => reject(signal.reason || new Error('aborted'));
      if (signal.aborted) {
        fail();
        return;
      }
      signal.addEventListener('abort', fail, { once: true });
    }),
  });
  const err = await rejected(() => turn.run());
  assert.match(String(err && err.message), /model turn failed \(504\)/);
  assert.equal(err.status, 504);
  assert.equal(err.andonPosted, true);
  assert.deepEqual(turn.responses, [{ status: 504, body: 'model turn timed out' }]);
  assert.equal(turn.andon.length, 1);
  assert.match(turn.andon[0], /^ANDON\|v=1\|phase=ANDON\|source=whatsapp-gateway\|kind=model_timeout\|status=504\|wamid=wamid.fixture\|lane=claude\|error=model turn timed out$/);
});

const dlq = require('../scripts/wa_dead_letter.js');

test('G16: a successful inbound persist completes before the 200 ACK', async () => {
  const a = inbound();
  await a.run([textMessage()]);
  assert.equal(a.requests.length, 1);
  assert.deepEqual(a.responses, [{ status: 200, body: 'EVENT_RECEIVED' }]);
  const kinds = a.timeline.map(e => e.kind);
  assert.deepEqual(kinds, ['persist', 'respond']);
  assert.equal(a.timeline[1].status, 200);
  assert.equal(a.timeline[1].requests, 1);
  assert.equal(a.p0s.length, 0);
});

test('G16: a persist failure returns non-200, writes a DLQ entry, and posts P0 on the first entry', async () => {
  const a = inbound();
  a.setResponse({ ok: false, json: async () => ({ result: 'success', rowId: 'WRK-fake' }) });
  const err = await rejected(() => a.run([textMessage('Project status')]));
  assert.match(String(err && err.message), /read back before replay/);
  assert.equal(a.saved.get('wamid.fixture'), undefined);
  assert.ok(a.responses.every(r => r.status !== 200), a.responses);
  assert.equal(a.responses[0].status, 503);
  const record = err.dlq;
  assert.equal(record.wamid, 'wamid.fixture');
  assert.equal(record.kind, 'persist_failed');
  assert.equal(record.retryCount, 1);
  assert.equal(record.firstEntry, true);
  assert.equal(a.p0s.length, 1);
  assert.match(a.p0s[0], /^P0\|v=1\|phase=P0\|class=ALERT\|source=whatsapp-gateway\|kind=dlq_first\|wamid=wamid.fixture\|retry_count=1\|status=503\|/);
});

test('G16: a later DLQ entry increments retry count and does not post a second P0', async () => {
  const a = inbound();
  a.setResponse({ ok: false, json: async () => ({ result: 'error', rowId: 'WRK-fake' }) });
  const first = await rejected(() => a.run([textMessage()]));
  assert.equal(first.dlq.retryCount, 1);
  assert.equal(a.p0s.length, 1);
  const second = await rejected(() => a.run([textMessage()]));
  assert.equal(second.dlq.retryCount, 2);
  assert.equal(second.dlq.firstEntry, false);
  assert.equal(a.p0s.length, 1);
});

test('G16: a failed model turn goes to the DLQ with a retry count and returns non-200', async () => {
  const turn = modelTurn({ callModel: async () => { throw new Error('provider rejected the request'); } });
  const err = await rejected(() => turn.run());
  assert.equal(err.status, 502);
  assert.equal(err.dlq.retryCount, 1);
  assert.equal(err.dlq.kind, 'model_error');
  assert.equal(err.dlq.lane, 'claude');
  assert.equal(err.p0Posted, true);
  assert.equal(turn.p0s.length, 1);
  assert.match(turn.p0s[0], /kind=dlq_first/);
  assert.deepEqual(turn.responses, [{ status: 502, body: 'model turn failed' }]);
});

test('G16: the first model DLQ entry posts P0; a retry of the same turn does not', async () => {
  const store = memoryStore();
  const turn = modelTurn({
    store,
    callModel: async () => { throw new Error('provider rejected the request'); },
  });
  const first = await rejected(() => turn.run());
  assert.equal(first.dlq.retryCount, 1);
  assert.equal(first.p0Posted, true);
  const second = await rejected(() => turn.run());
  assert.equal(second.dlq.retryCount, 2);
  assert.equal(second.dlq.firstEntry, false);
  assert.equal(second.p0Posted, false);
  assert.equal(turn.p0s.length, 1);
});

test('G16 P5: logs, DLQ and P0 contain no secret values and no raw message prose', async () => {
  const prose = 'please review the quarterly forecast for next week';
  const secret = 'sk-FIXTURESECRETVALUE';
  const turn = modelTurn({
    callModel: async () => { throw new Error('provider rejected: ' + prose + ' token=' + secret); },
  });
  const job = modelJob();
  job.input.message.text = prose;
  const err = await rejected(() => turn.run(job));
  const blobs = [
    JSON.stringify(err.dlq),
    turn.p0s.join('\n'),
    turn.logs.join('\n'),
    turn.andon.join('\n'),
    JSON.stringify(turn.responses),
  ].join('\n');
  assert.equal(blobs.includes(prose), false, blobs);
  assert.equal(blobs.includes(secret), false, blobs);
  assert.equal(blobs.includes('FIXTURESECRETVALUE'), false, blobs);
  assert.equal(blobs.includes('fixture-secret-value'), false, blobs);
  assert.match(err.dlq.error, /\[redacted\]/);
});

test('G16 P5: inbound logs and P0 rows omit secrets and raw prose', async () => {
  const prose = 'please review the quarterly forecast for next week';
  const a = inbound();
  a.setResponse({ ok: false, json: async () => ({ result: 'error' }) });
  const err = await rejected(() => a.run([textMessage(prose)]));
  const blobs = [JSON.stringify(err.dlq), a.p0s.join('\n'), a.logs.join('\n')].join('\n');
  assert.equal(blobs.includes(prose), false, blobs);
  assert.equal(blobs.includes('fixture-secret-value'), false, blobs);
  // The persist request body is the board append (not a log) and is not asserted here.
});

test('G16: dead-letter helper retry count and first-entry flag', async () => {
  const store = memoryStore();
  const a = await dlq.enqueueDeadLetter(store, { wamid: 'wamid.a', kind: 'model_error', status: 502, error: 'x' });
  assert.equal(a.retryCount, 1);
  assert.equal(a.firstEntry, true);
  const b = await dlq.enqueueDeadLetter(store, { wamid: 'wamid.a', kind: 'model_error', status: 502, error: 'x' });
  assert.equal(b.retryCount, 2);
  assert.equal(b.firstEntry, false);
  const c = await dlq.enqueueDeadLetter(store, { wamid: 'wamid.b', kind: 'model_timeout', status: 504, error: 't' });
  assert.equal(c.retryCount, 1);
  assert.equal(c.firstEntry, false);
  const payload = dlq.p0Payload(a);
  assert.match(payload, /^P0\|v=1\|phase=P0\|class=ALERT\|source=whatsapp-gateway\|kind=dlq_first/);
  assert.equal(payload.includes('|'), true);
});
