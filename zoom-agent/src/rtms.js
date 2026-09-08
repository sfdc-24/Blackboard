// Media plane: joins the RTMS stream when a meeting starts.
// The @zoom/rtms SDK handles the signaling/media WebSockets, HMAC signature,
// and protocol heartbeats — we just wire up data callbacks.
import rtms from '@zoom/rtms';
import { onTranscriptLine, onMeetingEnded } from './assistant.js';

const active = new Map(); // rtms_stream_id -> rtms.Client

export function handleZoomEvent({ event, payload }) {
  const obj = payload?.object ?? payload ?? {};
  const streamId = obj.rtms_stream_id;
  const meetingId = obj.meeting_uuid ?? streamId ?? 'unknown';

  switch (event) {
    case 'meeting.rtms_started':
      joinStream(streamId, meetingId, obj);
      break;

    case 'meeting.rtms_interrupted':
      // Transient drop — Zoom either resumes the stream or follows up with
      // rtms_stopped. Keep the client; just surface it.
      console.warn(`[rtms] stream interrupted for ${meetingId} — waiting for resume/stop`);
      break;

    case 'meeting.rtms_stopped': {
      const client = active.get(streamId);
      if (client) {
        client.leave();
        active.delete(streamId);
      }
      onMeetingEnded(meetingId);
      break;
    }

    default:
      // Not an RTMS event — ignore.
      break;
  }
}

function joinStream(streamId, meetingId, obj) {
  if (!streamId) {
    console.error('[rtms] rtms_started without rtms_stream_id — payload:', JSON.stringify(obj));
    return;
  }
  // Critical: only ONE connection per stream. A duplicate join kicks out the first.
  if (active.has(streamId)) {
    console.log(`[rtms] already connected to stream ${streamId} — skipping duplicate join`);
    return;
  }

  const client = new rtms.Client();
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
