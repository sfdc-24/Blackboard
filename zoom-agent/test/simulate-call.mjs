// Drives assistant.js with a scripted meeting so the live-assist path can be
// proven without a Zoom account, an RTMS entitlement, or a real client on the
// line. It calls the REAL SFDC24 reception() — the answers below came back over
// the wire, they are not fixtures.
//
//   node test/simulate-call.mjs
//
// WhatsApp and the board are intentionally left unconfigured: this proves the
// thinking half. Delivery is proven separately by scripts/wa_notify.ps1, which
// has been sending to the same number all week.
process.env.ZOOM_CLIENT_ID ??= 'test-client-id';
process.env.ZOOM_CLIENT_SECRET ??= 'test-client-secret';
process.env.ZOOM_WS_ENDPOINT ??= 'wss://example.invalid/ws?subscriptionId=test';
process.env.LOG_TRANSCRIPT ??= 'true';

// Read the endpoint from the environment rather than hardcoding it. The
// deployment id changes on every Apps Script redeploy, so a copy checked in
// here would rot, and a stale URL fails in a way that looks like a broken agent.
if (!process.env.SFDC24_EXEC) {
  console.error('\n✖ SFDC24_EXEC is not set.');
  console.error('  Put it in .env (see .env.example), or run:');
  console.error('    SFDC24_EXEC="https://script.google.com/macros/s/<id>/exec" node test/simulate-call.mjs\n');
  process.exit(1);
}

const { onTranscriptLine, onMeetingEnded } = await import('../src/assistant.js');

const MEETING = 'sim-' + Date.now().toString(36);

// A plausible discovery call. The wake word appears once, the way it would if
// he actually said it out loud mid-conversation.
const script = [
  ['Dana Okoye',  'Thanks for making time. The short version is our reporting is a mess.'],
  ['Abdus Salam', 'Tell me what happens on a Monday morning.'],
  ['Dana Okoye',  'Ops pulls the pipeline dashboard, then rebuilds it in a spreadsheet anyway.'],
  ['Dana Okoye',  'Nobody trusts the stage durations because the close dates get edited by hand.'],
  ['Marc Feliu',  'We also have about eleven flows on Opportunity and we are scared of all of them.'],
  ['Dana Okoye',  'The last admin left in 2023 and did not document anything.'],
  ['Abdus Salam', 'SFDC24, what should I be asking them about those flows right now?'],
];

console.log(`\n── simulated call ${MEETING} ─────────────────────────────\n`);

for (const [speaker, text] of script) {
  onTranscriptLine({ meetingId: MEETING, userName: speaker, text, ts: Date.now() });
  await new Promise((r) => setTimeout(r, 120));
}

// The wake-word ask is in flight; give it room, the way a real pause would.
console.log('\n[test] waiting for the live answer…\n');
await new Promise((r) => setTimeout(r, 40_000));

console.log('\n── call ends, wrap-up begins ───────────────────────────\n');
onMeetingEnded(MEETING);
await new Promise((r) => setTimeout(r, 50_000));
console.log('\n── done ────────────────────────────────────────────────\n');
