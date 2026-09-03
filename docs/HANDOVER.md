# Handover — state as of 2026-09-03

Written so a restarted session can pick up without re-deriving anything. Read
this first, then `MEMORY.md` in the memory directory.

## What is live right now

| Thing | State |
|---|---|
| Reception prompt | **Version 12** deployed. Humour removed; qualify-or-close in. Verified both directions. |
| sfdc24.com title | Fixed — `<title>` is now `SFDC24`, was `Home`. Published and verified. |
| Glasses capture loop | Running (`pythonw`, Startup shortcut). Untouched. |
| Board worker | **Not** scheduled, **not** running. Dormant by design. |
| Per-prompt log | Live via a **Stop hook**. Backfilled 99 prompts. |

## The three things that matter most, unfixed

1. **Anthropic console spend cap.** Two minutes, needs Mr. Salam, protects an
   endpoint that is already public. Repeatedly recommended, still not done.
2. **`google.script.run` exposes every function without a trailing underscore.**
   Cheapest live exploit. Gemini confirmed the mechanism. The audit is blocked
   because the browser extension will not let me read `Code.gs` — the planned
   workaround is a temporary diagnostic function that logs global names, then
   read the execution log. Gemini was asked whether that works in V8.
3. **Visitor text writes straight into the agent board.** ChatGPT rated it
   CRITICAL from production rows. Quarantine design agreed: `PUBLIC_INBOX` +
   provenance (`trust_level=EXTERNAL_UNTRUSTED`, `instruction_authority=NONE`).
   Gemini was asked for the code.

## Verified facts worth not re-deriving

- **Salesforce trademark policy does NOT name "SFDC"** — zero occurrences in
  their guidelines PDF. Gemini claimed it did; that was wrong. But the policy
  bans **"abbreviations … of any of Salesforce's trademarks"** and any
  "recognizable portion" in a **domain name**, which plausibly covers it. Verify
  before acting; this is interpretation, not a quoted prohibition.
- **WhatsApp does not deliver marketing templates to US phone numbers.**
  Confirmed on Meta's own docs (updated Jun 17 2026), error `131049`. **Canada
  is not covered** — the rule is "+1 with a US area code". Inbound still works
  everywhere. EEA/UK/Japan/Korea are exempt from the throttle.
- **The homepage has no indexable text** — everything is inside one iframe.
  Google Sites has no meta description field. Real words on the page is the only
  fix, which is why ChatGPT was asked to write the copy.
- **All four markets have free data including TSX** (Composite to 1979, S&P to
  1927). Intraday depth measured: 1m ≈ 8 days, 1h ≈ 1060 days. Delay
  **undetermined** — must be measured during a live session.
- **Six pre-registered trading hypotheses all failed** out-of-sample after costs.
  Real result, not a failed run.
- **Economics:** overhead is ~42% of job cost, agent tokens ~4%. Utilisation, not
  token efficiency, sets the price.

## Open agent conversations

Retrievable by URL; they hold context worth reusing.

- ChatGPT — site review, naming, business models, and (in flight) homepage copy:
  `https://chatgpt.com/c/6a9922ef-5f74-83e9-b99f-02ed4a0fa443`
  *Note: this tab freezes on long renders. Click the conversation in the sidebar
  to force a re-render; do not fight it with screenshots.*
- Gemini — architecture, open-source stack, demand, and (in flight) the
  quarantine/idempotency/audit code:
  `https://gemini.google.com/app/258380d5e09ea83c`
- Meta AI — WhatsApp platform reality:
  `https://www.meta.ai/prompt/00dad258-484e-458b-8733-254c0d6066af`

**There is no Meta model in the fleet.** The gateway's "Meta Llama" lane is Groq
serving `openai/gpt-oss-120b`, which self-reports as ChatGPT. Use meta.ai in the
browser for genuine Meta input.

## Naming

Ten candidates from ChatGPT, domains checked by me against the Verisign registry
(RDAP, definitive). **Eight of ten .com are available.** Taken:
`goodordersystems.com` (registered 2026-09-02, the day before we looked) and
`crmrepair.com`. Best of the list: **Ops Before Apps** (has a point of view) and
**Customer Ops Office** (safest). Its "PRELIM GREEN" trademark verdicts are
self-labelled guesses, not searches — CIPO and USPTO still needed.

**DECIDED 2026-09-03: keep `sfdc24.com` for now.** No rebrand, no domain bought.
The trademark exposure is *accepted, not resolved* — see the `naming-decision`
memory. Revisit on AppExchange submission, incorporation, ranking well, or any
contact from Salesforce. Standing recommendation for that day: two names, not a
rename — keep sfdc24.com for the practice, give the platform its own name.

## What I need from Mr. Salam — batched, ~6 minutes total

1. **Anthropic console → spend cap.** 2 min. Do this one regardless.
2. ~~Name decision~~ — **DONE. Keeping sfdc24.com.**
3. **Standing permission to edit the live Apps Script and Google Site without
   asking each time.** 30 sec. Currently I stop and ask; that is the main thing
   slowing autonomous work.
4. **Real numbers for `scripts/quote.py`, when convenient** (he has said later is
   fine): actual $/token from an invoice, his hourly rate, and a realistic
   jobs-per-month. The defaults are marked unverified and the tool says so.

## Tools built this session

```
trading/        fetch.py, backtest.py, research.py — data + honest backtesting
intake/         questions.json, README.md — the intake funnel and project record
scripts/prompt_log.py    per-prompt time and tokens, real data, Stop hook
scripts/quote.py         job estimator with overhead absorption and margin
web/sfdc24-cli.html      CLI-mode site prototype (unpublished)
```

**Local only, deliberately gitignored:** `docs/security-review.md`,
`docs/multi-agent-review-2026-09-03.md`. They map attack paths into a live
system. Mr. Salam had the security review force-pushed off the remote — keep
that rule.
