const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const load = require('./helpers/pipedream_loader.cjs');

const OPERATOR = '15550001234';
const textMessage = (text = 'Project status', id = 'wamid.fixture') => ({ id, from: OPERATOR, type: 'text', text: { body: text } });
const plain = value => JSON.parse(JSON.stringify(value));

function inbound() {
  const requests = [], saved = new Map(), exports = {};
  let response = { ok: true, json: async () => ({ result: 'success', rowId: 'WRK-fixture' }) };
  const env = { ALPHA_URL: 'https://example.invalid/board', ALPHA_SECRET: 'fixture', META_APP_SECRET: 'mock-app-secret' };
  const context = load('pipedream_wa_inbound.js', env, async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return response;
  });
  const props = { db: { get: async id => saved.get(id), set: async (id, value) => saved.set(id, value) } };
  return {
    requests, saved, exports, context,
    setResponse: value => { response = value; },
    async run(messages, extraValue = {}, mutateEvent = () => {}) {
      const body = { entry: [{ changes: [{ value: { ...extraValue, messages } }] }] };
      const raw_body = JSON.stringify(body);
      const signature = 'sha256=' + crypto.createHmac('sha256', env.META_APP_SECRET).update(raw_body).digest('hex');
      const event = { method: 'POST', body, raw_body, headers: { 'x-hub-signature-256': signature } };
      mutateEvent(event);
      await context.component.run.call(props, { steps: { trigger: { event } }, $: {
        respond: async () => {}, export: (name, value) => { exports[name] = plain(value); },
      } });
      return exports.summary;
    },
  };
}

function snapshot(rows) {
  return { spreadsheetId: '120_71KaF4JKGPGz0qUz4phqWRljSqEzSRRm_0zXC_oY', sheetId: 0,
    headers: ['Row_ID', 'Timestamp', 'Source_Tag', 'Target_Surface', 'Action_Type', 'Payload'],
    firstRow: 1170, fetchedAt: new Date().toISOString(), rows: rows || [
      ['view', '2026-09-06T16:41:23Z', 'vm-cli', 'ALL', 'APPEND', 'BCB|v=1|phase=VIEWPORT|vseq=011|by=vm-cli|state=work did not run'],
      ['auto', '2026-09-06T17:10:30Z', 'claude', 'ALL', 'RESULT', 'I have no running task'],
      ['new', '2026-09-06T17:30:36Z', 'claude-code-cli', 'ALL', 'APPEND', 'BCB|v=1|phase=RESULT|what=monitor email arrived'],
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
    assert.equal(a.saved.size, 0);
  }
});

test('configured signature verification fails closed on absent raw bytes or malformed signature', async () => {
  for (const mutate of [e => { delete e.raw_body; }, e => { e.headers['x-hub-signature-256'] = 'short'; }]) {
    const a = inbound();
    await assert.rejects(a.run([textMessage()], {}, mutate));
    assert.equal(a.requests.length, 0);
  }
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
  assert.deepEqual(plain(jobs[0].input.board.laterRows.map(r => r.id)), ['new']);
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
