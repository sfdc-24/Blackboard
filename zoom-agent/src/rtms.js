// Media plane: joins the RTMS stream when a meeting starts.
// The @zoom/rtms SDK handles the signaling/media WebSockets, HMAC signature,
// and protocol heartbeats — we just wire up data callbacks.
import { onTranscriptLine, onMeetingEnded } from './assistant.js';

// @zoom/rtms is a NATIVE module published for linux and darwin only. A static
// import here made this file — and therefore index.js, which imports it — fail
// to load on Windows with npm error notsup. That is why the agent could not be
// developed or tested on the laptop at all, and why it sat untouched from
// 29 August: the wall was in the first line of the module graph, not in the
// logic.
//
// It is now an OPTIONAL dependency loaded lazily, at the moment a stream
// actually starts. Everything else — the assistant, reception, the board and
// WhatsApp delivery — imports and tests on any platform. On a host without the
// SDK the failure is one clear sentence at the point of use rather than a
// module-resolution error at startup.
let rtmsModule = null;
async function loadRtms() {
  if (rtmsModule) return rtmsModule;
  try {
    rtmsModule = (await import('@zoom/rtms')).default;
    return rtmsModule;
  } catch (err) {
    throw new Error(
      '@zoom/rtms is not installed on this host. It publishes for linux and darwin '
      + 'only, so live media cannot run here — the rest of the agent still can. '
      + `Underlying error: ${err.message}`,
    );
  }
}

const active = new Map(); // rtms_stream_id -> rtms.Client

// async because joinStream now loads the SDK lazily. Callers may ignore the
// returned promise -- joinStream handles its own failures and never rejects --
// but awaiting it is what lets a test observe the outcome deterministically
// instead of racing a floating promise.
export async function handleZoomEvent({ event, payload }) {
  const obj = payload?.object ?? payload ?? {};
  const streamId = obj.rtms_stream_id;
  const meetingId = obj.meeting_uuid ?? streamId ?? 'unknown';

  switch (event) {
    case 'meeting.rtms_started':
      await joinStream(streamId, meetingId, obj);
      break;

    case 'meeting.rtms_interrupted':
      // Transient drop — Zoom either resumes the stream or follows up with
      // rtms_stopped. Keep the client; just surface it.
      console.warn(`[rtms] stream interrupted for ${meetingId} — waiting for resume/stop`);
      break;

    case 'meeting.rtms_stopped': {
      // A RESERVATION (null) must be cleared too, not just a live client. The
      // entry is `null` while the SDK is loading, and `if (client)` skipped the
      // delete for it — leaving a reservation nothing would ever remove, which
      // would silently refuse every future join of that stream id.
      if (active.has(streamId)) {
        const client = active.get(streamId);
        active.delete(streamId);
        if (client) client.leave();
      }
      onMeetingEnded(meetingId);
      break;
    }

    default:
      // Not an RTMS event — ignore.
      break;
  }
}

async function joinStream(streamId, meetingId, obj) {
  if (!streamId) {
    console.error('[rtms] rtms_started without rtms_stream_id — payload:', JSON.stringify(obj));
    return;
  }
  // Critical: only ONE connection per stream. A duplicate join kicks out the first.
  //
  // The check and the RESERVATION must happen in the same synchronous step. The
  // previous version checked, then awaited the lazy SDK import, then joined —
  // so two rtms_started events for the same stream arriving while the SDK was
  // loading BOTH passed the check, and both joined. The second kicked out the
  // first, which is the exact failure the guard exists to prevent, and it could
  // only happen on the very first stream of the process (the one time the
  // import is not already resolved).
  if (active.has(streamId)) {
    console.log(`[rtms] already connected to stream ${streamId} — skipping duplicate join`);
    return;
  }
  active.set(streamId, null);   // reservation: null means "joining"

  let rtms;
  try {
    rtms = await loadRtms();
  } catch (err) {
    active.delete(streamId);    // never hold a reservation we cannot fulfil
    console.error(`[rtms] cannot join stream ${streamId}: ${err.message}`);
    return;
  }
  // The stream may have STOPPED while the SDK was loading. If our reservation is
  // gone, someone cleared it deliberately and joining now would resurrect a
  // stream nobody is listening to.
  if (!active.has(streamId)) {
    console.log(`[rtms] stream ${streamId} was stopped while the SDK loaded — not joining`);
    return;
  }

  let client;
  try {
    client = new rtms.Client();
  } catch (err) {
    active.delete(streamId);    // a reservation we cannot fulfil is a dead lock
    console.error(`[rtms] could not construct a client for ${streamId}: ${err.message}`);
    return;
  }
  active.set(streamId, client);

  let audioFrames = 0;
  let videoFrames = 0;
  let shareFrames = 0;

  client.onJoinConfirm((reason) => {
    console.log(`[rtms] join confirm for ${meetingId} (reason=${reason})`);
  });

  // ── Transcript → the AI assistant hook (highest-value stream for live assistance)
  client.onTranscriptData((buf, ts, metadata) => {
    onTranscriptLine({
      meetingId,
      userName: metadata?.userName,
      text: buf.toString('utf8'),
      ts,
    });
  });

  // ── Audio (raw frames — wire to your own STT / voice analysis if needed)
  client.onAudioData((buf, _ts, metadata) => {
    if (++audioFrames % 500 === 1) {
      console.log(`[audio] ${audioFrames} frames (latest from ${metadata?.userName ?? 'mixed'}, ${buf.length}B)`);
    }
  });

  // ── Video (H.264 frames of the active speaker by default)
  client.onVideoData((buf, _ts, _trackId, metadata) => {
    if (++videoFrames % 250 === 1) {
      console.log(`[video] ${videoFrames} frames (latest from ${metadata?.userName ?? '?'}, ${buf.length}B)`);
    }
  });

  // ── Screen share (separate stream from video — slide/screen understanding goes here)
  client.onShareData((buf, _ts, metadata) => {
    if (++shareFrames % 100 === 1) {
      console.log(`[share] ${shareFrames} frames (from ${metadata?.userName ?? '?'}, ${buf.length}B)`);
    }
  });

  // ── In-meeting chat
  client.onChatData((buf, _ts, metadata) => {
    console.log(`[chat] ${metadata?.userName ?? '?'}: ${buf.toString('utf8')}`);
  });

  client.onSharingEvent((ev, _ts, _userId, userName) => {
    console.log(`[rtms] sharing ${ev} by ${userName ?? '?'}`);
  });

  client.onLeave((reason) => {
    console.log(`[rtms] left ${meetingId} (reason=${reason})`);
    active.delete(streamId);
  });

  console.log(`[rtms] joining media stream for ${meetingId}…`);
  client.join({
    meeting_uuid: obj.meeting_uuid,
    rtms_stream_id: obj.rtms_stream_id,
    server_urls: obj.server_urls,
    signature: obj.signature,
  });
}
