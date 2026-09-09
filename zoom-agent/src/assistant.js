// Where the "AI" in the AI consultant lives.
//
// It buffers the live transcript per meeting and, when asked, sends recent
// context to SFDC24 and puts the answer on the consultant's phone while he is
// still on the call. When the call ends it leaves an evidence-backed finding on
// the blackboard — which is this project's stated mission, not a nice-to-have.
//
// THINGS THIS DELIBERATELY DOES NOT DO:
//
//  1. It does not answer every line. A suggestion every N lines would buzz his
//     phone through a client call and he would turn the whole thing off. The
//     default is WAKE mode: it speaks when spoken to. Periodic mode exists,
//     is off by default, and still refuses to fire while he is mid-sentence.
//
//  2. It does not call a model directly. Everything goes through SFDC24's
//     reception(), so the Zoom agent shares one persona and one set of spend
//     caps with the website and WhatsApp. See reception.js.
//
//  3. It does not send more than the backend accepts. `q` is cut at 1,000
//     characters server-side, so a whole call is summarised in bounded chunks
//     and whatever did not fit is COUNTED and disclosed. See prompt.js.
//
//  4. It does not claim delivery it cannot evidence. WhatsApp acceptance is
//     counted as `accepted`, and the board finding is reported only after the
//     row is read back by Row_ID.
import { config } from './config.js';
import { ask, newConversation } from './reception.js';
import { sendWhatsApp, whatsappConfigured } from './notify.js';
import { appendRow, bcb, boardConfigured } from './board.js';
import { buildBounded, planSummaryChunks } from './prompt.js';

const sessions = new Map(); // meetingId -> session

function newSession(meetingId) {
  return {
    meetingId,
    // Server-minted conversation token lives here, so reception() keeps
    // context — and per-conversation budget — across every ask in the meeting.
    conv: newConversation(),
    lines: [],
    startedAt: new Date(),
    asks: 0,
    accepted: 0,   // messages WhatsApp accepted. NOT deliveries: see notify.js.
    failures: [],
    // The in-flight live ask, as a PROMISE rather than a boolean. A boolean can
    // be tested but not awaited, and the wrap-up needs to wait: see
    // onMeetingEnded.
    pending: null,
    lastAskAtLine: 0,
  };
}

export function onTranscriptLine({ meetingId, userName, text, ts }) {
  const line = text?.trim();
  if (!line) return;

  const session = sessions.get(meetingId) ?? newSession(meetingId);
  session.lines.push({ userName: userName ?? 'unknown', text: line, ts });
  sessions.set(meetingId, session);

  if (config.logTranscript) {
    console.log(`[transcript] ${userName ?? 'unknown'}: ${line}`);
  }

  const trigger = detectTrigger(line, session);
  if (!trigger) return;

  // Fire and forget: the media stream must never wait on a network call.
  handleAsk(session, trigger)?.catch((err) =>
    console.error('[assistant] ask failed:', err.message));
}

function detectTrigger(line, session) {
  const lower = line.toLowerCase();

  // WAKE: someone addressed the assistant out loud.
  const hit = config.wakeWords.find((w) => lower.includes(w));
  if (hit) {
    // Strip the wake word so the question reads naturally to the backend.
    // Deliberately NOT a RegExp: wake words are operator-supplied, and one
    // containing a metacharacter would either mis-match ("sfdc24.com" matching
    // "sfdc24Xcom") or throw and take transcript processing down with it.
    // indexOf/slice cannot do either.
    const at = lower.indexOf(hit);
    const question = (line.slice(0, at) + line.slice(at + hit.length))
      .replace(/^[\s,:.–—-]+/, '')
      .trim();
    return { kind: 'wake', question: question || line };
  }

  // PERIODIC: opt-in, and only between utterances so it does not interrupt.
  if (config.assistEveryN > 0 &&
      session.lines.length - session.lastAskAtLine >= config.assistEveryN) {
    return { kind: 'periodic', question: null };
  }
  return null;
}

const asLines = (lines) => lines.map((l) => `${l.userName}: ${l.text}`);

function handleAsk(session, trigger) {
  // One outstanding request per meeting. Live speech arrives faster than a
  // model answers, and without this a busy call stacks up a queue of stale
  // questions that all arrive at once after the moment has passed.
  if (session.pending) return null;
  session.lastAskAtLine = session.lines.length;
  const run = runAsk(session, trigger).finally(() => {
    if (session.pending === run) session.pending = null;
  });
  session.pending = run;
  return run;
}

async function runAsk(session, trigger) {
  const context = asLines(session.lines.slice(-config.contextLines));

  const header = trigger.kind === 'wake'
    ? [
        'You are assisting live during a Zoom call. Answer in at most three',
        'sentences that can be read on a phone mid-conversation. If you do not',
        'know, say so plainly rather than guessing.',
        '',
        `The question: ${trigger.question}`,
        '',
        'Recent transcript for context:',
      ]
    : [
        'You are listening to a live Zoom call. From the transcript below, is',
        'there ONE thing the consultant should know or ask right now? Answer in',
        'at most two sentences. If there is nothing worth interrupting for,',
        'reply with exactly: NOTHING',
        '',
      ];

  // Oldest context is dropped until it fits. A live question is about what
  // was just said, so the tail is the part worth keeping.
  const { text: prompt, dropped } = buildBounded(header, context);
  if (dropped > 0) {
    console.log(`[assistant] context trimmed to fit the 1,000-char contract (${dropped} older lines dropped)`);
  }

  session.asks += 1;
  const { ok, reply, error } = await ask(prompt, session.conv);

  if (!ok) {
    session.failures.push(error ?? 'unknown');
    console.error(`[assistant] reception failed: ${error}`);
    return;
  }

  // The periodic gate: the model is allowed to decide there is nothing to say.
  if (trigger.kind === 'periodic' && /^\s*nothing\b/i.test(reply)) {
    console.log('[assistant] periodic check — nothing worth interrupting for');
    return;
  }

  const prefix = trigger.kind === 'wake' ? 'Asked on the call' : 'Heads up';
  await deliver(session, `${prefix}\n\n${reply}`);
}

/** Hand a message to WhatsApp, or say plainly that there is nowhere to send it. */
async function deliver(session, message) {
  if (!whatsappConfigured()) {
    console.log(`[assistant] (no delivery channel configured)\n${message}`);
    return;
  }
  const sent = await sendWhatsApp(message);
  if (sent.ok) {
    session.accepted += 1;
    // "accepted", not "delivered" — Graph returning a wamid means it took the
    // message, not that a handset received it.
    console.log(`[assistant] WhatsApp accepted the message (${sent.id})`);
  } else {
    session.failures.push(sent.error);
    console.error(`[assistant] WhatsApp handoff failed: ${sent.error}`);
  }
}

export function onMeetingEnded(meetingId) {
  const session = sessions.get(meetingId);
  if (!session) return Promise.resolve();
  sessions.delete(meetingId);

  const minutes = Math.max(1, Math.round((Date.now() - session.startedAt) / 60000));
  console.log(`[assistant] meeting ${meetingId} ended — ${session.lines.length} lines, ${session.asks} asks, ${session.accepted} accepted`);

  if (session.lines.length === 0) return Promise.resolve();

  // WAIT for any live ask still in flight before summarising. Both use the same
  // mutable conversation, and on the very first request neither has a `ct` yet —
  // so two concurrent calls would each be minted a different server token and
  // the later segments would lose continuity. A late WhatsApp result would also
  // change `accepted` after the board payload had already captured it, putting
  // a number on the board that was wrong the moment it was written.
  //
  // A failed live ask must not cancel the wrap-up, hence the swallowed catch.
  //
  // The chain is RETURNED. Callers may ignore it — this never rejects — but
  // returning it is what lets a test observe the ordering deterministically
  // rather than racing a floating promise. Same reasoning as rtms.js.
  return Promise.resolve(session.pending)
    .catch(() => {})
    .then(() => wrapUp(session, minutes))
    .catch((err) => console.error('[assistant] wrap-up failed:', err.message));
}

const SUMMARY_TASK = [
  'That Zoom call has ended. From the transcript, produce:',
  '1) three sentences on what the call was actually about,',
  '2) the action items, each with an owner if one was named,',
  '3) anything said about a Salesforce org that is worth checking later.',
  'Be specific. If something was not discussed, leave it out rather than padding.',
  '',
];

// This must serve EVERY section of SUMMARY_TASK, not just the actionable ones.
// The first version asked only for decisions, action items and org details, and
// offered a NOTHING escape — so a discovery call that decided nothing mapped
// every segment to NOTHING, left zero notes, and abandoned the wrap-up
// entirely. A conversation with no decisions is still a call worth summarising,
// and "what this part was about" is the first thing the final summary asks for.
const SEGMENT_TASK = [
  'This is ONE SEGMENT of a longer Zoom call, in order. In at most three short',
  'sentences, capture what a summary of the whole call would need from it:',
  'first what this part was ABOUT, then any decisions, any action items with',
  'owners, and anything said about a Salesforce org. Always describe the',
  'subject, even when nothing was decided.',
  '',
];

/**
 * Summarise the call within the contract.
 *
 * One request when the call fits. Otherwise map-reduce: each segment is
 * summarised inside the 1,000-char budget, then the notes are reduced into the
 * final summary. Segment count is capped because every segment is a paid
 * backend call — and whatever the cap leaves out is reported rather than
 * quietly missing.
 */
async function summarise(session) {
  const lines = asLines(session.lines.slice(-config.summaryLines));
  const single = buildBounded(SUMMARY_TASK, lines);

  if (single.dropped === 0) {
    session.asks += 1;
    const res = await ask(single.text, session.conv);
    return { ...res, covered: res.ok ? single.used : 0, total: session.lines.length, segments: 1 };
  }

  const plan = planSummaryChunks(SEGMENT_TASK, lines, { maxChunks: config.summaryMaxChunks });
  if (plan.chunks.length === 0) {
    return { ok: false, reply: null, error: 'nothing summarisable', covered: 0, total: session.lines.length, segments: 0 };
  }

  // Coverage is credited per segment, and only to segments whose note both
  // came back AND survived the reduction. Reporting plan.covered regardless
  // told WhatsApp and the board that the summary covered lines whose only
  // intermediate representation had been thrown away — the same "claims more
  // than it has" defect the bounding work exists to remove, one level up.
  const kept = [];
  for (const chunk of plan.chunks) {
    session.asks += 1;
    const res = await ask(chunk.text, session.conv);
    if (!res.ok) {
      session.failures.push(res.error ?? 'segment summary failed');
      continue;
    }
    kept.push({ note: res.reply, lines: chunk.lines });
  }
  if (kept.length === 0) {
    return { ok: false, reply: null, error: 'every segment summary failed', covered: 0, total: session.lines.length, segments: plan.chunks.length };
  }

  const reduced = buildBounded(SUMMARY_TASK, kept.map((k) => k.note));
  // buildBounded keeps the NEWEST notes that fit, so the survivors are the tail.
  const survived = kept.slice(kept.length - reduced.used);
  const covered = survived.reduce((n, k) => n + k.lines, 0);

  session.asks += 1;
  const res = await ask(reduced.text, session.conv);
  return {
    ...res,
    covered: res.ok ? covered : 0,
    total: session.lines.length,
    segments: plan.chunks.length,
    segmentsKept: survived.length,
    notesDropped: kept.length - survived.length,
  };
}

async function wrapUp(session, minutes) {
  const { ok, reply, covered, total, segments, segmentsKept = segments, notesDropped = 0 } = await summarise(session);

  if (!ok) {
    console.error('[assistant] could not summarise the call');
    return;
  }

  const speakers = [...new Set(session.lines.map((l) => l.userName))];
  // Coverage is stated on the summary itself, not buried. A summary of 60% of a
  // call that presents as a summary of the call is the failure this replaced.
  // NOT "the last N lines": when a middle segment fails, what is covered is no
  // longer contiguous, and describing it as a tail would be a second false
  // claim layered on the first.
  const coverage = covered >= total ? `all ${total} lines` : `${covered} of ${total} lines`;
  const segmentNote = segments > 1
    ? ` across ${segmentsKept === segments ? `${segments} segments` : `${segmentsKept} of ${segments} segments`}`
    : '';
  const header = `Call ended — ${minutes} min, ${total} transcript lines, ${speakers.length} speaker(s)\n`
    + `Summary covers ${coverage}${segmentNote}.`;

  if (whatsappConfigured()) {
    const sent = await sendWhatsApp(`${header}\n\n${reply}`);
    if (sent.ok) session.accepted += 1;
    console.log(sent.ok
      ? '[assistant] summary accepted by WhatsApp'
      : `[assistant] summary handoff failed: ${sent.error}`);
  } else {
    // Without this the wrap-up is computed and then silently dropped, which is
    // exactly what the first simulated call did. A summary nobody can read is
    // the same as no summary.
    console.log(`[assistant] (no delivery channel configured)\n${header}\n\n${reply}`);
  }

  // The finding. Evidence-backed means it says what it is derived from and what
  // it is not: a transcript is what was said, not what is true in the org.
  if (!boardConfigured()) return;

  const boundary = [
    'Derived from the live transcript only. Nothing here was verified against a Salesforce org,',
    'and speech-to-text mishears names and identifiers - confirm before acting.',
    covered >= total
      ? `Summary covers all ${total} transcript lines.`
      : `Summary covers ${covered} of ${total} transcript lines; the other ${total - covered} were outside the summarisation budget or their segment summary did not survive.`,
    notesDropped > 0 ? `${notesDropped} segment notes did not fit the final reduction and are NOT counted as covered.` : '',
    segments > 1 && segmentsKept < segments ? `${segments - segmentsKept} of ${segments} segments contributed nothing to this summary.` : '',
    `WhatsApp accepted ${session.accepted} message(s); acceptance is not proof of delivery to the handset.`,
  ].filter(Boolean).join(' ');

  const payload = bcb({
    id: callId(session.startedAt),
    phase: 'RESULT',
    class: 'FINDING',
    from: 'zoom-agent',
    to: 'claude-code-cli',
    cc: 'ALL',
    attest: 'zoom-agent/sfdc24-reception/FROM-TRANSCRIPT-ONLY',
    duration_min: minutes,
    transcript_lines: total,
    summary_covered_lines: covered,
    speakers: speakers.length,
    live_asks: session.asks,
    accepted: session.accepted,
    errors: session.failures.length ? session.failures.slice(0, 3).join('; ') : 'none',
    summary: reply,
    evidence_boundary: boundary,
  });

  const row = [
    cryptoId(),
    new Date().toISOString(),
    'zoom-agent',
    'Blackboard Alpha DB',
    'APPEND',
    payload,
    // A RESULT is finished work. Marking it OPEN put completed findings into
    // every reader's outstanding queue.
    'DONE',
    'ZOOM-AGENT',
    `Live call assisted: ${minutes} min, ${session.asks} asks, ${session.accepted} accepted by WhatsApp`,
    'Transcript-derived. Not verified against any org.',
  ];

  const res = await appendRow('Blackboard - Alpha DB', row);
  if (res.verified) {
    console.log(`[assistant] finding on the board, read back once by Row_ID ${res.rowId}`);
  } else if (res.ambiguous) {
    console.error(`[assistant] board write UNRESOLVED: ${res.error}`);
  } else {
    console.error(`[assistant] board write failed: ${res.error}`);
  }
}

/** Collision-resistant even when two calls end in the same minute — the old id
 *  was a 15-char slice of an ISO timestamp, so simultaneous meetings collided. */
function callId(startedAt) {
  const stamp = startedAt.toISOString().replace(/[-:.]/g, '').slice(0, 15);
  return `ZOOM-CALL-${stamp}-${randomSuffix()}`;
}

function randomSuffix() {
  return (globalThis.crypto?.randomUUID?.() ?? Math.random().toString(16).slice(2))
    .replace(/-/g, '').slice(0, 8);
}

function cryptoId() {
  return (globalThis.crypto?.randomUUID?.()) ??
    `zm-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
