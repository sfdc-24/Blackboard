# Gateway patch — make model failures VISIBLE

**Target:** Pipedream workflow `SFDC 24 - WhatsApp to Blackboard`, step
`send_whatsapp_reply`. Written 2026-09-03 by claude-code-cli.

## The defect

Every model task catches its own error and **returns it as a string**.
`Promise.allSettled` then joins those strings into `agentResponseText`, and the
step returns `status: "Success"` with HTTP 200.

So a failed model produces a **green checkmark** in the Pipedream event list.

That is not theoretical. Gemini was dead for days — every call aborted at the
12-second timeout — and every one of those runs showed as successful. Nobody
noticed because nothing looked wrong. The same shape has now appeared three
times in this system:

| Where | Symptom |
|---|---|
| v1 bus (REQ-V8QD7R) | `ok:true` for an append that wrote nothing |
| v1 bus (ISSUE 020 / REQ-C5NBX2) | failure-shaped response on a write that landed |
| this gateway | `Success` + 200 while delivering `[Gemini Error]` |

Fixing this is worth more than fixing any individual model, because it is what
lets the *next* failure be noticed in minutes instead of days.

## What the patch does NOT do

It does **not** stop the reply being sent. The working models' answers still
reach the user, and the board row is still written. The failure is raised
**after** all delivery, so the run is marked failed without losing output.

Safe because **"Automatically retry on errors" is unchecked** in this
workflow's settings (verified 2026-09-03). Throwing at the end therefore
cannot cause a duplicate WhatsApp send or a duplicate board row. **If you ever
enable retries, revisit this patch first.**

---

## Step 1 — declare a failure list

Near the top of `run()`, beside the other `let` declarations (around the
`agentResponseText` declaration):

```js
// Structured record of lane failures. Deliberately NOT string-sniffing the
// reply text: the reply is for humans and its wording will drift.
const failures = [];
```

## Step 2 — record in every catch and every API-error branch

Each task already has a `catch (e)` that returns a string. Add one line
**before** each return. Do this for all four lanes:

```js
} catch (e) {
  failures.push({ lane: "Gemini", error: String(e && e.message || e) });   // <-- add
  agentResponseText = `[Gemini Error]: ${e.message}`;
}
```

```js
} catch (e) {
  failures.push({ lane: "ChatGPT", error: String(e && e.message || e) });  // <-- add
  return `*ChatGPT:* Error (${e.message})`;
}
```

```js
} catch (e) {
  failures.push({ lane: "Claude", error: String(e && e.message || e) });   // <-- add
  return `*Claude:* Error (${e.message})`;
}
```

```js
} catch (e) {
  failures.push({ lane: "MetaLlama", error: String(e && e.message || e) }); // <-- add
  return `*Meta Llama:* Error (${e.message})`;
}
```

Also the **API-error** branches that return without throwing — the Gemini
`if (data.error)` and the Groq `if (data.error)`:

```js
if (data.error) {
  failures.push({ lane: "Gemini", error: data.error.message || "API error" });
  agentResponseText = `[Gemini API Error ${data.error.code || ""}]: ${data.error.message}`;
}
```

## Step 3 — fix the Claude lane's silent failure (do this in the same pass)

The Claude branch reads `data.content`, filters text blocks, and falls back to
the literal string `"No response"`. **It never checks `data.error`.** So an
exhausted balance, a bad key, a rate limit or a wrong model id all render as
those two bland words. That cost an evening on 2026-09-02.

The Groq lane already does this correctly. Copy it — add immediately after
`const data = await res.json();` in the Claude task:

```js
if (data.error) {
  failures.push({ lane: "Claude", error: data.error.message || "API error" });
  return `*Claude:* [API Error]: ${data.error.message}`;
}
```

And make the empty case a recorded failure rather than a shrug:

```js
const textBlocks = (data.content || []).filter(b => b.type === "text").map(b => b.text);
const joined = textBlocks.join(" ").trim();
if (!joined) {
  failures.push({ lane: "Claude", error: "empty content array — no text blocks returned" });
  return `*Claude:* [Empty response]`;
}
return `*Claude:* ${joined}`;
```

## Step 4 — surface it, then fail the run

At the **very end** of `run()`, AFTER the WhatsApp reply has been sent and
AFTER the board row has been written — order matters, do not move this earlier:

```js
// Exports are recorded even when the step throws, so the detail survives.
$.export("lane_failures", failures);
$.export("lanes_ok", 4 - failures.length);

if (failures.length) {
  // Fail the RUN so the event list shows red and error notifications fire
  // ("Send error notifications" is already enabled on this workflow).
  // Delivery has already happened above; this only changes visibility.
  throw new Error(
    `${failures.length} of 4 model lanes failed — ` +
    failures.map(f => `${f.lane}: ${f.error}`).join(" | ")
  );
}

return { status: "Success", userPrompt, agentResponseText, lanes_ok: 4 };
```

---

## How to verify it works

1. Deploy (**Manage deployments → edit existing → Version: New version**).
2. Temporarily break one lane — easiest is setting the Gemini timeout back to
   something tiny like `1500`.
3. Send a WhatsApp message containing **`benchmark`** (the literal keyword is
   required to fan out to all four lanes — see `DEPLOY.md`).
4. Expected: the reply still arrives with the three working answers, **and**
   the Pipedream event shows **red** with
   `1 of 4 model lanes failed — Gemini: This operation was aborted`.
5. Restore the timeout to `30000` and redeploy.

Step 4 is the whole point. Before this patch that run showed a green check.

## Follow-on worth doing later

Once failures are visible, the natural next step is a **dead-letter lane**: on
any failure, append a board row with `phase=ERROR` carrying the lane and the
message. Then failures are queryable history rather than something you have to
be watching Pipedream to catch. Not required for this patch.
