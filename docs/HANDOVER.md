# Handover — 2026-09-03

Read this first, then `MEMORY.md` in the memory directory. Written so a
restarted session resumes without re-deriving anything.

---

## Security fix — DEPLOYED AND VERIFIED 2026-09-03

**Version 13 is live.** The four editor utilities that any visitor could call
are now guarded.

`post_reset()` was the serious one: it appends a reset row that blanks the live
site's project pages while forging `by=Governor`. `seed_state()`, `test_chat()`
and `test_read()` were also open.

**How it was verified, not assumed.** An anonymous request to the Governor-only
endpoint returns `{"ok":false,"error":"not authorized"}`, which proves
`whoami_().isGovernor` is `false` for visitors — so `requireGovernor_()` throws
for all four. That is the same check `getState` and `postRow` already relied on
in production, so it permits the owner. No exploitation needed to confirm it.
Reception still serves (200, `google.script.run` present) and the homepage copy
is still live.

**Known automation limit, recorded so nobody retries it:** "New version" in the
Manage-deployments dropdown cannot be selected by automation — confirmed across
two browsers, two viewport sizes, mouse and keyboard. Worse, the Deploy that
completes then silently re-publishes the SAME version while reporting
"Deployment successfully updated". Always read the version number. Mr. Salam
clicked it by hand.

---

## What is live right now## What is live right now

| Thing | State |
|---|---|
| Reception prompt | **Version 12.** Humour removed, qualify-or-close in. Verified both directions. |
| Homepage copy | **LIVE and verified.** sfdc24.com went from ~0 to **3,502 indexable characters**. |
| Site title | `SFDC24` (was `Home`). Published. |
| Exposed-function guards | **DEPLOYED — Version 13. Verified.** |
| PUBLIC_INBOX quarantine | **Written in repo, inert, NOT in project.** See below. |
| Glasses capture loop | Running (`pythonw`, Startup shortcut). Untouched. |
| Board worker | Not scheduled, not running. Dormant by design. |
| Per-prompt log | Live via Stop hook. |

---

## Verified facts — do not re-derive these

**Rate limits DO exist** (previously flagged unverified):
`CHAT_SESSION_CAP = 12`, `CHAT_DAILY_DEFAULT = 150`, `CHAT_MAX_INPUT = 1000`,
plus `CHAT_ENABLED` as a kill switch. Cost-exhaustion is bounded. Model is
`claude-sonnet-4-5`.

**Clickjacking is confirmed, not suspected** — `setXFrameOptionsMode(ALLOWALL)`
at Code.gs line 58. Required for the Google Sites embed, so a deliberate trade,
but any site can frame the reception.

**The exposed-function audit, complete.** Nine functions in Code.gs are callable
via `google.script.run`. Five are fine — `doGet`, `doPost`, `getState`,
`postRow`, `reception` are entry points or check authorisation internally.
`getState` carries the comment *"scope enforced here, not by obscurity"*, which
is correct. The four unguarded ones were `post_reset`, `seed_state`,
`test_chat`, `test_read`.

**WhatsApp does not deliver marketing templates to US numbers.** Confirmed on
Meta's own docs (updated Jun 17 2026), error `131049`. **Canada is not covered**
— the rule is "+1 with a US area code". Inbound works everywhere. EEA/UK/Japan/
Korea are exempt from the throttle. So WhatsApp is dead for US outbound and fine
for inbound, which is what the intake funnel actually needs.

**Salesforce trademark policy does NOT name "SFDC"** — zero occurrences in their
guidelines PDF. Gemini claimed it did; that was wrong. But the policy bans
**"abbreviations … of any of Salesforce's trademarks"** and any "recognizable
portion" in a **domain name**. Interpretation, not a quoted prohibition.
**Decision: keeping sfdc24.com. Exposure accepted, not resolved.**

**There is no Meta chat model on the Groq account.** The only Meta models
reachable are `llama-prompt-guard-2` (22m/86m), tiny safety classifiers. The
gateway lane labelled "Meta Llama" is `openai/gpt-oss-120b`.

**Six pre-registered trading hypotheses all failed** out-of-sample after costs.
A real result. 19 names are worth 3.6–10.8 *independent* observations, so the
Six Sigma n≈20 rule does not transfer to correlated markets.

**Economics:** overhead is ~42% of job cost, agent tokens ~4%. Utilisation, not
token efficiency, sets price.

---

## The quarantine rewire — one line, do it with the deploy

`scripts/public_inbox_quarantine.gs` is written and **inert**. It was
deliberately not wired because nothing could be deployed or tested from that
session, and rewiring a live logging path you cannot verify or roll back is
reckless.

To apply: paste the file into the project as a new `.gs`, then replace the body
of `logVisitor_` in Code.gs with:

```js
function logVisitor_(sid, who, text) { logVisitorQuarantined_(sid, who, text); }
```

That reroutes all six call sites at once. Then send one reception message and
confirm a row lands in `PUBLIC_INBOX` and **not** on the board.

**Hazard already avoided, do not undo it:** the quarantine sheet's first column
is `Inbox_ID`, **not** `Row_ID`. `sheet_()` finds the operational board by
scanning for a `Row_ID` header, so a second sheet with that header could make it
bind to the wrong one and break every board write.

---

## Open agent conversations — reuse, do not restart

- **ChatGPT** — site review, naming, business models, homepage copy:
  `https://chatgpt.com/c/6a9922ef-5f74-83e9-b99f-02ed4a0fa443`
  *Freezes on long renders. Click the conversation in the sidebar to force a
  re-render; do not fight it with screenshots.*
- **Gemini** — architecture, open-source stack, demand, quarantine/idempotency:
  `https://gemini.google.com/app/258380d5e09ea83c`
- **Meta AI** — WhatsApp platform reality:
  `https://www.meta.ai/prompt/00dad258-484e-458b-8733-254c0d6066af`

**Groq works now** (`scripts/ask_groq.py`). 14 models; best chat model is
`openai/gpt-oss-120b`. Use it for bulk work — free and fast (3.4s for a
1,500-token critique).

---

## Strategy — what the round-table settled

**All four models independently said: abandon ecommerce, fitness and games; go
deeper on Salesforce.** Training/education is the one real bridge (ChatGPT and I
say real, Gemini said no — 2:1).

**Groq broke the plan we were about to build, correctly.** Automating the
diagnostic destroys what made it sellable — it becomes a lead magnet, not a
product. **Give the diagnostic away, charge downstream** for interpretation,
implementation and monitoring. `intake/` needs revising for this.

**New product direction nobody else raised:** make the audit trail *the
product*, not a consulting differentiator — a recurring API for buyers who need
auditability. **But the honest first step is tamper-evident logging.** Groq
claimed the trail has "cryptographic hashes"; it does not. It is a Google Sheet
with UUIDs.

**Groq's own bias, for calibration:** it assumes distribution is free because
production is free. Ten micro-products and "autopilot sales" presume an audience
that does not exist. Selling is the binding constraint, not building.

---

## Still open

1. Quarantine rewire — still to do; one line in `logVisitor_`, needs its own
   deploy (Mr. Salam has to click "New version" by hand).
2. **No proof on the homepage.** Four problem statements, zero evidence. Caps
   conversion until one anonymised engagement exists.
3. Every homepage section is a problem statement — a reader can agree four times
   without learning what they receive. The (now free) diagnostic offer belongs
   on the page.
4. Idempotency: duplicate Governor events confirmed 1.7s apart. **Gemini's
   verdict: CacheService is insufficient, LockService is required** — it is
   eventually consistent, so two executions both pass `cache.get()`.
5. Bus secret rotation (deferred by Mr. Salam). `codegs_rotation_window.gs`
   exists to make it zero-downtime.
6. Human gate (`scripts/human_gate.gs`) still not wired.
7. Real numbers for `scripts/quote.py` — rates are marked UNVERIFIED.
8. llm-wiki v0.24.4 is installed and enabled; its commands load on restart.
   `archive`, `checkpoint`, `ingest`, `librarian` are the relevant ones. The
   Obsidian vault is `C:\Users\salam\My Drive\SFDC 24 - Claude` — its 98 files
   are `.gdoc`/`.gsheet` **pointers Obsidian cannot read**, which is why it
   indexes as empty. Real markdown is in `Claude Archive/`.

---

## Tools built

```
trading/     fetch.py, backtest.py, research.py   market data + honest backtesting
intake/      questions.json, README.md            intake funnel and project record
scripts/prompt_log.py       per-prompt time and tokens, real data, Stop hook
scripts/quote.py            job estimator with overhead absorption
scripts/ask_groq.py         free bulk offload
scripts/public_inbox_quarantine.gs   quarantine (inert)
scripts/audit_exposed_functions.gs   REST-API function audit (unused; editor search sufficed)
web/sfdc24-cli.html         CLI-mode site prototype (unpublished)
prompts/consensus-attack.md the prompt that broke our own consensus
```

**Local only, gitignored, keep it that way:** `docs/security-review.md`,
`docs/multi-agent-review-2026-09-03.md`. They map attack paths into a live
system. The security review was force-pushed off the remote at his instruction.

---

## Standing doctrine (also in memory)

- **No Anthropic spend cap.** Declined; never raise it again. No auto-reload is
  the ceiling. Just report if credits run out.
- **Standing permission** to edit the live Apps Script and Google Site without
  asking. DNS is *not* covered — I broke the site with it once.
- **Delegate first** to ChatGPT/Gemini/Groq, have them error-proof each other,
  and **verify every claim against a primary source** before acting. Several
  were wrong today.
- **Batch questions** with time estimates. He is bottlenecked by availability,
  not willingness.
