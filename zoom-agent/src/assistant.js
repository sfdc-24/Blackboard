// Where the "AI" in the AI consultant lives.
//
// It buffers the live transcript per meeting and, when asked, sends recent
// context to SFDC24 and puts the answer on the consultant's phone while he is
// still on the call. When the call ends it leaves an evidence-backed finding on
// the blackboard — which is this project's stated mission, not a nice-to-have.
//
// TWO THINGS THIS DELIBERATELY DOES NOT DO:
//
//  1. It does not answer every line. A suggestion every N lines would buzz his
//     phone through a client call and he would turn the whole thing off. The
//     default is WAKE mode: it speaks when spoken to. Periodic mode exists,
//     is off by default, and still refuses to fire while he is mid-sentence.
//
//  2. It does not call a model directly. Everything goes through SFDC24's
//     reception(), so the Zoom agent shares one persona and one set of spend
//     caps with the website and WhatsApp. See reception.js.
import { config } from './config.js';
import { ask } from './reception.js';
import { sendWhatsApp, whatsappConfigured } from './notify.js';
import { appendRow, bcb, boardConfigured } from './board.js';

const sessions = new Map(); // meetingId -> session

function newSession(meetingId) {
  return {
    meetingId,
    // Stable per meeting so reception() keeps conversational context across asks.
    conversationId: 'z' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
    lines: [],
    startedAt: new Date(),
    asks: 0,
    delivered: 0,
    failures: [],
    inFlight: false,
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
  handleAsk(session, trigger).catch((err) =>
    console.error('[assistant] ask failed:', err.message));
}

function detectTrigger(line, session) {
  const lower = line.toLowerCase();

  // WAKE: someone addressed the assistant out loud.
  const hit = config.wakeWords.find((w) => lower.includes(w));
  if (hit) {
    // Strip the wake word so the question reads naturally to the backend.
    const question = line.replace(new RegExp(hit, 'i'), '').replace(/^[\s,:.–—-]+/, '').trim();
    return { kind: 'wake', question: question || line };
  }

  // PERIODIC: opt-in, and only between utterances so it does not interrupt.
  if (config.assistEveryN > 0 &&
      session.lines.length - session.lastAskAtLine >= config.assistEveryN) {
    return { kind: 'periodic', question: null };
  }
  return null;
}

async function handleAsk(session, trigger) {
  // One outstanding request per meeting. Live speech arrives faster than a
  // model answers, and without this a busy call stacks up a queue of stale
  // questions that all arrive at once after the moment has passed.
  if (session.inFlight) return;
  session.inFlight = true;
  session.lastAskAtLine = session.lines.length;

  try {
    const context = session.lines.slice(-config.contextLines);
    const transcript = context.map((l) => `${l.userName}: ${l.text}`).join('\n');

    const prompt = trigger.kind === 'wake'
      ? [
          'You are assisting live during a Zoom call. Answer in at most three',
          'sentences that can be read on a phone mid-conversation. If you do not',
          'know, say so plainly rather than guessing.',
          '',
          `The question: ${trigger.question}`,
          '',
          'Recent transcript for context:',
          transcript,
        ].join('\n')
      : [
          'You are listening to a live Zoom call. From the transcript below, is',
          'there ONE thing the consultant should know or ask right now? Answer in',
          'at most two sentences. If there is nothing worth interrupting for,',
          'reply with exactly: NOTHING',
          '',
          transcript,
        ].join('\n');

    session.asks += 1;
    const { ok, reply, error } = await ask(prompt, session.conversationId);

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
    const message = `${prefix}\n\n${reply}`;

    if (whatsappConfigured()) {
      const sent = await sendWhatsApp(message);
      if (sent.ok) {
        session.delivered += 1;
        console.log(`[assistant] delivered to WhatsApp (${sent.id})`);
      } else {
        session.failures.push(sent.error);
        console.error(`[assistant] WhatsApp delivery failed: ${sent.error}`);
      }
    } else {
      console.log(`[assistant] (no delivery channel configured)\n${message}`);
    }
  } finally {
    session.inFlight = false;
  }
}

export function onMeetingEnded(meetingId) {
  const session = sessions.get(meetingId);
  if (!session) return;
  sessions.delete(meetingId);

  const minutes = Math.max(1, Math.round((Date.now() - session.startedAt) / 60000));
  console.log(`[assistant] meeting ${meetingId} ended — ${session.lines.length} lines, ${session.asks} asks, ${session.delivered} delivered`);

  if (session.lines.length === 0) return;
  wrapUp(session, minutes).catch((err) =>
    console.error('[assistant] wrap-up failed:', err.message));
}

async function wrapUp(session, minutes) {
  const transcript = session.lines
    .slice(-config.summaryLines)
    .map((l) => `${l.userName}: ${l.text}`)
    .join('\n');

  const { ok, reply } = await ask([
    'That Zoom call has ended. From the transcript, produce:',
    '1) three sentences on what the call was actually about,',
    '2) the action items, each with an owner if one was named,',
    '3) anything said about a Salesforce org that is worth checking later.',
    'Be specific. If something was not discussed, leave it out rather than padding.',
    '',
    transcript,
  ].join('\n'), session.conversationId);

  if (!ok) {
    console.error('[assistant] could not summarise the call');
    return;
  }

  const speakers = [...new Set(session.lines.map((l) => l.userName))];
  const header = `Call ended — ${minutes} min, ${session.lines.length} transcript lines, ${speakers.length} speaker(s)`;

  if (whatsappConfigured()) {
    const sent = await sendWhatsApp(`${header}\n\n${reply}`);
    console.log(sent.ok ? '[assistant] summary sent to WhatsApp' : `[assistant] summary delivery failed: ${sent.error}`);
  } else {
    // Without this the wrap-up is computed and then silently dropped, which is
    // exactly what the first simulated call did. A summary nobody can read is
    // the same as no summary.
    console.log(`[assistant] (no delivery channel configured)\n${header}\n\n${reply}`);
  }

  // The finding. Evidence-backed means it says what it is derived from and what
  // it is not: a transcript is what was said, not what is true in the org.
  if (boardConfigured()) {
    const payload = bcb({
      id: `ZOOM-CALL-${session.startedAt.toISOString().replace(/[:.]/g, '').slice(0, 15)}`,
      phase: 'RESULT',
      class: 'FINDING',
      from: 'zoom-agent',
      to: 'claude-code-cli',
      cc: 'ALL',
      attest: 'zoom-agent/sfdc24-reception/FROM-TRANSCRIPT-ONLY',
      duration_min: minutes,
      transcript_lines: session.lines.length,
      speakers: speakers.length,
      live_asks: session.asks,
      delivered: session.delivered,
      errors: session.failures.length ? session.failures.slice(0, 3).join('; ') : 'none',
      summary: reply,
      evidence_boundary: 'Derived from the live transcript only. Nothing here was verified against a Salesforce org, and speech-to-text mishears names and identifiers - confirm before acting.',
    });

    const row = [
      cryptoId(),
      new Date().toISOString(),
      'zoom-agent',
      'Blackboard Alpha DB',
      'APPEND',
      payload,
      'OPEN',
      'ZOOM-AGENT',
      `Live call assisted: ${minutes} min, ${session.asks} asks, ${session.delivered} delivered to phone`,
      'Transcript-derived. Not verified against any org.',
    ];

    const res = await appendRow('Blackboard - Alpha DB', row);
    // D-4: this is not proof. It is what the bus said.
    console.log(res.ok
      ? '[assistant] finding posted to the board (read back to confirm)'
      : `[assistant] board write reported: ${res.error} — DO NOT blind-retry, read back first`);
  }
}

function cryptoId() {
  return (globalThis.crypto?.randomUUID?.()) ??
    `zm-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
