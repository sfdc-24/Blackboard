# WhatsApp context, message identity and interactive-input repair

The September 6 board records show Mr. Salam asking for actual project updates
and named instances while automatic gateway replies offer generic templates.
Two inbound rows have empty payloads; Claude CLI reports they followed interactive
taps. These components prepare the corresponding repair. They have **offline
contract evidence only**, and have not been deployed to Pipedream.

## What is ready

- `scripts/pipedream_wa_inbound.js` preserves original message ID, quoted-message
  ID, exact text, and button/list option ID plus label. The board text keeps its
  existing `WA|wamid=...` prefix and final `|text=...` field. Structured exports
  carry these fields separately, so routing never needs to parse the user's text
  as protocol delimiters. Empty or malformed input throws before its append.
- An append must return successful HTTP plus the known gateway JSON contract
  (`result: success`, nonempty `WRK-...` rowId) before the message is recorded in
  the Data Store. This is an acknowledgement, not independent row read-back.
  Raw response bodies are no longer exported. Configured signature validation
  refuses absent raw bytes and consumes the exact signed JSON body.
- `scripts/pipedream_wa_context.js` requires a verified inbound export, the
  configured operator's sender ID, and a fresh snapshot of the exact Alpha DB
  tab. It selects the newest structurally identifiable VIEWPORT and later
  instance/WhatsApp rows, excluding gateway `claude`/`gemini` echoes. Context
  omissions and per-row truncation are explicit. Board claims remain quoted
  data; neither source tags nor ORDER text confer execution authority.
- Explicit `claude-code-cli:`, `codex:`, `vm-cli:`, `vm-chrome:` and `chat-mobile:`
  addresses produce a labelled gateway acknowledgement without a model call or
  a claim that the named instance has started. Other messages produce distinct
  model jobs with the grounded context and a gateway identity instruction.
- `scripts/pipedream_wa_reply.js` builds the Meta POST body for each job. The
  recipient and `context.message_id` come from the prepared inbound identity,
  never model text. Model responses carry `[STATUS | gateway/<model>]`; actual
  persistent instances should use their own tag, such as `[STATUS | codex]`.

The context and reply steps perform no network calls and no side effects.
They do not wake workers, send WhatsApp messages or execute board instructions.

## Inspect the existing workflow before wiring

The VM had no Pipedream connector or configured Pipedream credential, and the
browser opened the sign-in/marketing surface rather than the workflow. The
deployed `send_whatsapp_reply` source and its current props/retry settings could
not be read. Therefore the following wiring must be completed against an
export/read-back of that existing workflow; the component files alone are not
a drop-in whole-workflow replacement.

Use one coordinated staging change in `SFDC 24 - WhatsApp to Blackboard`:

1. Export the currently deployed source, step bindings, worker concurrency and
   retry settings. Preserve a restorable version before changing them. Inspect
   the current append endpoint response against the contract above; do not
   weaken acknowledgement checks to accept arbitrary HTML or HTTP 200.
2. Replace the inbound code with the reviewed component. Bind its required Data
   Store and retain `ALPHA_URL`, `ALPHA_SECRET`, `META_APP_SECRET` through the
   existing environment configuration. Configure a fresh `META_VERIFY_TOKEN`;
   the legacy literal was removed from source and should not be reused. The
   trigger must expose the original raw body. The private context step will
   refuse `signature != valid`, even though the legacy inbound-only mode can
   still run without an app secret.
3. Add a read-only Google Sheets step for the already-authorized Alpha DB, tab
   Sheet1 (sheetId 0). Read the six headers and a bounded tail containing the
   newest used row and newest VIEWPORT, at most 500 rows. Determine the newest
   used row through a bounded timestamp/ID scan first; grid capacity is not a
   last-used-row marker. Never issue a read to `ALPHA_URL`: that gateway appends
   on calls that look like reads. Do not substitute PUBLIC_INBOX.
4. Map the authenticated inbound step's `summary` export and the fresh read into
   the context component's `inbound` and `snapshot` props. Set `WA_OPERATOR_ID`
   from the existing verified operator recipient, without moving credentials.
   Configure the model lane explicitly. The snapshot shape is shown below.
5. Iterate **every** returned job in order. For `callModel: false`, skip all model
   calls and use the prepared response. For `callModel: true`, pass `job.system`
   to the provider's system-instruction field and JSON-serialize `job.input` as
   a separate user/context input. Do not promote any board text into the system
   field. Preserve the existing provider error checks and timeouts.
6. Feed that exact job plus its successful model text to the reply component.
   Send its returned body through the existing Meta messages POST, retaining
   configured business number/token. Do not let a model select recipients or
   supply the reply message ID. Stop the old parallel auto-reply branch from
   sending a second, ungrounded response for the same event.

Snapshot example (values supplied by the fresh connector read, not hardcoded):

```json
{
  "spreadsheetId": "120_71KaF4JKGPGz0qUz4phqWRljSqEzSRRm_0zXC_oY",
  "sheetId": 0,
  "headers": ["Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type", "Payload"],
  "firstRow": 1170,
  "fetchedAt": "<UTC timestamp of this read>",
  "rows": [["<row id>", "<timestamp>", "<source tag>", "<target>", "<action>", "<payload>"]]
}
```

The adapter must include the latest used row and retain true row numbers. The
context component checks identity/shape and a five-minute fetch-age bound; it
cannot independently prove a caller-supplied snapshot is complete. An absent
VIEWPORT, stale snapshot or wrong operator fails visibly instead of silently
falling back to a generic model with no project context. Up to 24,000 characters
of serialized row records are retained, plus metadata; a fresh fetch does not
make the old VIEWPORT's reported status current.

## Delivery and retry limits

Pipedream Data Stores are not atomic or transactional. The inbound get/set pair
does not prove exactly-once processing; use a single workflow worker for this
store and retain idempotency/read-back at the destination. A crash after a board
write but before saving its receipt still needs read-back, not a blind replay.
The existing early webhook ACK is not a durable work queue. A later failure
must surface through Pipedream's event/error handling and a deliberate recovery
procedure; do not assume Meta will retry a delivery already acknowledged.
[Pipedream Data Store semantics](https://pipedream.com/docs/workflows/data-management/data-stores)

These changes do not add durable outgoing-send deduplication. Keep automatic
retries disabled around non-idempotent sends until the inspected workflow has
a verified outbox/recovery design. A Meta accepted message ID is submission
evidence; delivered/read status needs its webhook or the recipient's confirmation.
Do not replay today's historical production events as test fixtures.

## Acceptance for the actual staged workflow

The offline suite (`node --test tests/test_whatsapp_gateway.cjs`) exercises the
actual component code with mocked I/O. It covers button/list identity, Unicode
and delimiter preservation, quoted IDs, duplicate and status-only deliveries,
append failures, environment-only GET verification, case-insensitive signature
headers, operator isolation, bounded context, gateway labels and per-message
response correlation.

Live staging still needs a fresh text message, a button tap, a list choice and
a quoted reply. For each, compare the received Meta event ID and choice ID with
the exact board row and prepared response's `context.message_id`; confirm one
correctly labelled response reaches the operator. A CLI-addressed request must
not claim a worker started or that gateway acceptance is sheet read-back. Verify
the GET handshake with the freshly configured token before reconnecting Meta.
Test failed appends and stale context against safe
fixtures, and verify they create no misleading processed receipt or blank reply.
Record the deployed component hashes, bindings and delivery evidence before
retiring the old branch. No live interactive-input or delivery repair is claimed
by the offline tests.

References: [Pipedream step exports](https://pipedream.com/docs/workflows),
[Node.js code-step props](https://pipedream.com/docs/workflows/building-workflows/code/nodejs),
[Meta's Cloud API collection](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api),
[Meta's contextual reply interface](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/messages/interactive/).
