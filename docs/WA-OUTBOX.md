# Blackboard → WhatsApp outbox

A surface can send WhatsApp **if and only if its host holds the Meta
credentials** (`docs/COMMS-PROTOCOL.md` §1). Most agents do not. The
laptop already does: `scripts/wa_notify.ps1` plus a gitignored `.env`
(`META_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_TO`).

This is the durable path that does **not** put `META_TOKEN` in Apps
Script or Drive (doctrine **D-18**).

```
agent posts a board row          laptop watcher (this)
phase=WA_SEND / WA_OUT|    →     scripts/wa_board_outbox.py
Tag = agent id                   calls wa_notify.ps1 -Tag <from> -Text <body>
Text = message body              recipient is always WA_TO (Governor)
```

`wa_notify.ps1` refuses a recipient argument. The outbox does not add
one. There is no path here to anyone except Mr. Salam.

A row is not a wake-up (`COMMS-PROTOCOL` §7). Something on the laptop
has to poll. Arm `scripts/wa_board_outbox.ps1` in Task Scheduler.

---

## 1 · What an agent posts

**Preferred — BCB with `phase=WA_SEND`:**

```
BCB|v=1|id=WA-<tag>-<yyyymmddThhmmZ>|phase=WA_SEND|from=<your-tag>|to=wa-outbox|kind=STATUS|
<message body, no extra pipes if you can help it>
```

Write it with `scripts/append.py` or `bus.ps1 -SheetRowJson` as a
native ten-cell A:J row. `Source_Tag` (column C) must be your tag.
`from=` should match it. Gist (column I) is the one-line summary the
fleet notification shows — put the same sentence there.

| field | value |
|---|---|
| `phase=` | `WA_SEND` |
| `from=` / `Source_Tag` | the agent id (`claude-code-cli`, `codex`, `grok-bot`, …) |
| body | `text=` / `body=`, or a trailing bare segment after the header |
| `kind=` | optional: `STATUS` (default), `ASK`, `BLOCKED`, `ANDON`, `DONE` |
| `to=` | board addressing only (`wa-outbox`). **Not a phone number.** |

**Also accepted — payload prefix:**

```
WA_OUT|<message body>
```

`Source_Tag` is then the agent id. Use this only when you cannot emit
a BCB envelope. Prefer `phase=WA_SEND`.

**Do not** put a phone number, `WA_TO`, or `META_TOKEN` on the row.
The watcher will not send to anyone except the Governor number already
in the laptop `.env`.

### Examples

Claude / Codex / Copilot, same shape, different `from=`:

```
BCB|v=1|id=WA-CLAUDE-20260921T0015Z|phase=WA_SEND|from=claude-code-cli|to=wa-outbox|kind=ASK|
Need a yes/no on landing the outbox PR.
```

```
BCB|v=1|id=WA-CODEX-20260921T0016Z|phase=WA_SEND|from=codex|to=wa-outbox|kind=STATUS|
PR 50 review is waiting on the clasp credential.
```

```
WA_OUT|Copilot finished the dry-run; no send was attempted.
```

(with `Source_Tag=copilot` on that last one)

---

## 2 · What the laptop watcher does

On each pass `scripts/wa_board_outbox.py`:

1. Reads recent board rows through `scripts/bus.py` (`match=WA_SEND`
   and `match=WA_OUT`, `limit=80`). Bus credentials only. No Meta token.
2. Parses `phase=WA_SEND` and `WA_OUT|` rows.
3. Skips anything already in `logs/wa_board_outbox_state.json`, or
   already quoted by a `phase=NOTE` row (`answers=` / `delivered=`).
4. Calls `wa_notify.ps1 -Tag <from> -Kind <kind> -TextFile <body>`.
   The body file is how `-Text` is passed without putting prose on
   argv (the same lesson as `board_say.py`).
5. On Graph success, records the Row_ID (and BCB `id=`) in the state
   file **before** anything else. Optional `--note` then appends a
   `phase=NOTE` receipt. A failed NOTE does not re-send.

**First run primes.** An empty state file records every matching row
already on the board and sends nothing. Re-run to send rows posted
after that.

Idempotent: a second pass with the same rows sends zero.

---

## 3 · Dry-run (no Graph, no state write)

From a fixture (CI and any host without `.env`):

```powershell
python scripts\wa_board_outbox.py --dry-run --fixture tests\fixtures\wa_outbox_rows.json
```

Against the live board, still no send:

```powershell
python scripts\wa_board_outbox.py --dry-run
```

`list` is the same as dry-run: pending rows only.

```powershell
python scripts\wa_board_outbox.py list
```

A dry-run prints `pending=N` and one line per request. It does not
write the state file and it does not call Graph.

---

## 4 · Arming the watcher on the laptop

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\wa_board_outbox.ps1 --dry-run
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\wa_board_outbox.ps1
```

Schedule the second form (Task Scheduler, every few minutes while the
laptop is up). The wrapper finds Python and runs `once`.

`.env` on that machine already holds `BUS_URL`, `BUS_SECRET`,
`META_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_TO`. Do not copy any of them
into this repo, a board row, or Apps Script.

---

## 5 · What this is not

- Not a way to message anyone except the Governor.
- Not an inbound WhatsApp path (`scripts/grok_wa_inbox.py` / Pipedream).
- Not a reason to store `META_TOKEN` in Drive or the bus project.
- Not a wake-up: if the laptop task is not running, the row waits.
