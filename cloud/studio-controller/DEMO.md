# SFDC24 guided milestone demonstration

Run these cases in order. Before each case, state the evidence level exactly:
`offline`, `staging`, or `live`. Do not promote synthetic or mocked evidence to
live. Record the UTC time, release SHA, environment, and resulting receipt.

## 1. Email-code authentication

- Say or type: `Hey, this is Salam. Activate Blackboard or authenticate me.`
- Expected UI: one focused email question, then a six-digit code question.
- Preconditions: allowlisted lowercase email, deployed Apps Script sender,
  controller HMAC secrets, and a synthetic-worker Cloud Run revision.
- Self-test: request a code, read it from the real delivered email, verify it
  once, then prove the same code cannot be reused.
- Receipt: challenge ID, delivery time, verification time, opaque operator
  subject, controller revision. Never record the email address or code.

## 2. Direct Blackboard conversation

- Ask: `What can you help me change on SFDC24 tonight?`
- Expected UI: authenticated Studio session, ordered dialogue boxes, current
  prototype, and the next decision shown together.
- Preconditions: valid operator token and a newly created idempotent session.
- Self-test: reconnect using the session token and confirm SSE resumes without
  duplicating an event or consuming another admission.
- Receipt: session ID, generation, last sequence, artifact version, admission
  number, and release SHA.

## 3. Typed rapid prototype change

- Ask: `Create a History page direction with a welcoming hero and one clear action.`
- Expected UI: visible prototype change followed by a dialogue box explaining
  the change and asking only the next decision.
- Preconditions: Phase 2 synthetic flow or a separately approved provider
  worker. Clearly label which one is active.
- Self-test: replay the same command ID and prove it returns the same receipt;
  alter the payload under that ID and prove it receives HTTP 409.
- Receipt: command ID, artifact version before/after, event IDs, affected
  artifact IDs, and worker name.

## 4. Female welcoming voice

- Say: `Welcome me, then ask which page I want to improve.`
- Expected UI/audio: the same committed controller text appears in dialogue and
  is spoken with the configured female voice; Stop ends both immediately.
- Preconditions: Phase 3 voice gate, OpenAI Realtime key, scheduled cleanup,
  real microphone/speaker check, and successful compensating-hangup test.
- Self-test: establish one real WebRTC call, transcribe one genuine utterance,
  press Stop during the call, and verify provider hangup plus durable cleanup.
- Receipt: voice ID, provider call ID, expiry, transcript item ID, Stop command
  receipt, cleanup result. Never store SDP or audio.

## 5. Inspiration, options, and agent vote

- Ask: `Show me three homepage inspirations, explain the tradeoffs, and have the agents vote.`
- Expected UI: three visual directions, consequences, one recommendation with
  rationale, named agent votes, and a clear selection control.
- Preconditions: approved design-source policy and agent-vote contract.
- Self-test: select a direction, confirm only the affected prototype nodes
  change, then use `change decision` and prove the revision increases.
- Receipt: decision batch ID, option IDs, votes, selected option, revision, and
  artifact patch IDs.

## 6. WhatsApp continuation

- Ask in WhatsApp: `Continue my SFDC24 Studio session.`
- Expected response: secure short-lived link to the authenticated Studio state;
  do not claim native WhatsApp screen sharing.
- Preconditions: verified WhatsApp Business gateway, account binding, consent,
  expiring continuation link, and redacted logging.
- Self-test: open the link once, verify session continuity, then prove expiry or
  revocation blocks reuse.
- Receipt: gateway message ID, opaque account binding, link expiry, resumed
  session ID, and controller sequence.

## 7. Zoom listen, speak, and present

- Say in the meeting: `Zoom agent, show the current SFDC24 prototype and explain the last change.`
- Expected meeting behavior: the Ubuntu GCE presenter shares the designated
  browser, speaks the committed response, and never exposes terminals/secrets.
- Preconditions: presenter health check, meeting admission, selected share
  window, audio route, and host consent.
- Self-test: genuine meeting audio in, matching transcript, one spoken response,
  and visible prototype share from the designated window.
- Receipt: meeting ID hash, presenter VM revision, transcript item ID, spoken
  response ID, and share-window title.

## 8. Live OmniStudio Lead count

- Ask: `How many leads are in the OmniStudio development org?`
- Expected response: `The OmniStudio development org has N Lead records`, with
  org alias and query time. Never substitute fixture data.
- Preconditions: authenticated least-privilege Salesforce connection and live
  aggregate query permission.
- Self-test: execute `SELECT COUNT() FROM Lead` through the governed connector
  and compare the spoken/text value to the same-org query receipt.
- Receipt: org ID hash, org alias, SOQL hash, count, query timestamp, and API
  request ID.

The source-only authenticated controller canary can exercise the text/API part
of this case after a dedicated `lead-canary` Cloud Run service exists. Run
`tools/authenticated_canary.py` only with explicit `--live`, the exact
`https://www.sfdc24.com` Origin, and the reviewed canonical `.a.run.app` canary
origin. Enter the email, operator-supplied Lead expectation, and
OTP only through its non-echoing prompts. Its redacted receipt proves the
baseline snapshot, exact expectation comparison, committed SSE event, stable
command replay with no new event, replayable Stop, terminal SSE event, and a
fresh post-Stop HTTP 410. Label this `controller_api_canary`; it does not prove
an immutable Salesforce org identity, served page, browser interaction, email
UX, voice, or deployment. The
offline MockTransport suite is only an `offline pass`, never a live result.

## 9. Salesforce object or field change

- Ask: `Plan a new field on Lead for prototype interest.`
- Expected flow: plan, metadata diff, impact/risk summary, explicit confirmation,
  development-org deployment, and org read-back. No free-form direct mutation.
- Preconditions: authorized dev org, Metadata API permission, namespace/name
  validation, rollback package, and a confirmation bound to the exact diff.
- Self-test: deploy the confirmed metadata only, query the org definition, and
  show the resulting field in the UI or schema response.
- Receipt: plan ID, confirmation ID, metadata package hash, deployment ID,
  component result, org read-back, and rollback reference.

## Demonstration closeout

For every case mark exactly one outcome: `live pass`, `staging pass`, `offline
pass`, `blocked`, or `not started`. A case is not a live pass until the real
external service and user-visible path have both been exercised. Continue to
the next independent case when a connector is blocked; do not erase or inflate
the completed increments.
