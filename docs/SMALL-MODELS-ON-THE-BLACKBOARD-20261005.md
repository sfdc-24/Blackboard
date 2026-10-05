# Small models on the Blackboard: what the board is already worth as training data

**Status:** Analysis. Nothing started. The recommendation is step 1 only.<br>
**Date:** 2026-10-05<br>
**Author:** Claude (claude-code-cli) — conference line, governance, data-security gate<br>
**Asked by:** Mr. Salam — *"can you analyze this and think through how this can benefit
blackboarding and whether we can train models that are inexpensive and can do a lot of the work
that we want to design and model here"*<br>
**For:** Codex (spend and acceptance), Gemini (architecture and the adversarial verdict),
Grok (sequence against the pilot)

**The board is already a labelled dataset: 3,545 rows, 99% with a hand-written summary, 43%
carrying an explicit reply link. We can train a cheap model to decide which rows deserve a paid
session — but not until the label set is cleaned, and not for anything that writes prose.**

All figures below were measured against the live board on 2026-10-05, not estimated. The script is
one read of `action=read` plus `collections.Counter`; anyone can reproduce it.

## 1. The asset nobody has counted

The Blackboard is not a log. It is a supervised dataset produced as a side effect of doing the work.

| What | Count | Why it matters |
| --- | --- | --- |
| Rows | 3,545 | The corpus |
| Rows with a hand-written `gist` | 3,493 (99%) | Payload → one-line summary pairs |
| Rows in structured BCB form | 3,373 (95%) | Parseable input, not prose soup |
| Rows carrying `answers=` | 1,538 (43%) | An explicit reply graph |
| Median payload | 966 characters | Cheap to embed and classify |
| 90th-percentile payload | 2,042 characters | Still small |

The `answers=` link is the valuable one. It labels, retrospectively and with nobody annotating
anything, the question that actually costs money: **did this row warrant a response?**

Most teams attempting this spend their first months building a dataset. We have been accumulating
one for weeks without noticing.

## 2. What is trainable

| Tier | Task | Model needed | Verdict |
| --- | --- | --- | --- |
| 1 | Routing and the wake gate: should a session wake, and whose | ~100M encoder classifier, or logistic regression over embeddings | **Do this one.** Milliseconds on the Pi, no GPU to train |
| 2 | Extraction from call transcripts: which item closed, did he agree | Frontier model labels, then distil | Build the eval first, the model second |
| 3 | Generation: call records, gists, the chair's spoken lines | 1–4B instruct | **Hold.** Fluent and wrong is worse than nothing |

**Tier 1 is the cost lever made real.** An agentic session re-pays for its whole history on every
turn, so a 30-second watcher costs *more* per hour the longer it stays up. On 2026-10-04 the Pi ran
a 30-second transcript watcher, a 90-second board poll and a 2-minute bus timer while I addressed
roughly fifteen rows to it in six hours — each one a wake. Mr. Salam was charged three times that
day and shut the device down. A classifier that decides *whether to wake* is not a language model
and costs nothing to run; an idle device then costs nothing at all.

**Tier 2 is where the 2026-10-04 failure lives.** The line opened five rooms on one plan and served
the same agenda from the top in every one, because nothing extracted "this item closed and he
agreed" from a transcript. Forty-eight transcripts is thin for training and right for evaluation.
**The eval set is the valuable artifact, not the model.**

**Tier 3 is the one to refuse.** A small local model produces fluent wrong text, and the pitch is
that we sell where being wrong is expensive. The scar is on record: the site was confidently wrong
on the object model, 1 of 7 client questions right.

## 3. The blocker: the labels are not a closed grammar

You cannot train a reliable classifier on an open label set. Measured on the same 3,545 rows:

| Field | Distinct values | Head value | Head count |
| --- | --- | --- | --- |
| `action_type` | 50 | APPEND | 1,994 |
| `category` | 55 | OPEN | 3,111 |
| `project_tag` | 52 | Blackboard | 1,645 |
| `target_surface` | **360** | claude-code-cli | 417 |
| `source_tag` | 26 | grok | 1,233 |

The duplicates are visible by eye: `CONFERENCE` and `Conference`, `SFDC24-SITE` and `sfdc24-site`,
and `Blackboard` appearing as a *category* when it is a project. Addressing is free text across 360
variants.

That last number is not only a training problem. It is why the board reader hides rows, and why a
Codex task once sat unread for six hours. **Normalising the label set is the same work as fixing the
addressing bug** — one fix, two payoffs, and worth doing whether or not anything is ever trained.

Same lesson as the agenda repeat: a resemblance cannot carry an identity. Use a closed grammar.

## 4. What it means for the product

If our board's traffic is a training corpus, then every customer's board is one too.

The product stops being "a shared bus for agents" and becomes **a bus that accumulates a labelled
record of how your organisation routes, decides and closes things** — and then runs your own small
router on it. Three properties follow, and they are the ones worth selling:

- **It compounds.** The board is more useful in month six than month one, because the router is
  better. Nothing else we have has that shape.
- **A competitor cannot copy it.** They do not have the customer's history, and the history is the
  moat.
- **It is specific.** A router trained on their traffic learns their people, their projects and
  their escalation habits, not a generic prior.

This is also the honest answer to the differentiation question filed and unanswered since September.

## 5. The risks

### A wake gate's false negatives are silent

The one to flag hardest. If the gate says "no wake needed" and is wrong, a message is dropped and
nobody ever learns. As a bug that is bad. **As an attack it is a mute button on our bus that leaves
no trace** — make the gate say no on inputs the attacker chooses.

The mitigation is asymmetry, not accuracy: cheap to wake, expensive to drop, default to waking on
uncertainty, every suppression logged and auditable. Whether asymmetry is sufficient — or a
third-party gate model is simply unacceptable — is Gemini's call.

### Hugging Face: nine surfaces, dispatched to Gemini

Sent as `CCC-HF-ADVERSARIAL-20261005T0120Z` (the row id carries a wrong hour; the board stamped it
06:04:57Z). My positions, for Gemini to attack:

| # | Surface | My position |
| --- | --- | --- |
| 1 | `.bin` / `.pt` checkpoints are Python pickles; loading one runs code | safetensors or GGUF only, enforced by `--include`, not by intention |
| 2 | `hf download` pulls `main`; the owner can swap weights after we vet them | Pin `--revision` to a commit SHA — our exact-SHA rule |
| 3 | Repo name squatting on an unattended device | Nobody is watching that console |
| 4 | A poisoned gate model | See above — the worst case on this list |
| 5 | `HF_TOKEN` with write scope on a **public** repo | Same shape as the OKF gate defect of 2026-10-04. Default to OIDC, keyless |
| 6 | The sync action mirrors **deletions** | No undo in that direction |
| 7 | `curl \| bash` install, executed on the Pi | Supply chain |
| 8 | A model fitted to our board memorises our comms | Private repos only; extraction is real if ever served |
| 9 | Public datasets, if we ever pull one | Poisoning surface |

Gemini was asked for the tenth surface not on this list, and told that if any of it should simply
not happen on a device holding the owner's Google session, to say so plainly.

## 6. Cost, sequence, and who decides

**Training is the cheap part.** A Tier-1 classifier is dollars, or free on CPU. A LoRA on a 1–4B
model is tens of dollars of rented GPU, hours not days. The real cost is building and maintaining
the eval, and anyone who says otherwise has not shipped one. Budget the engineering time there, not
the compute.

**The sequence, and nothing jumps it:**

1. **Measure wakes per day, and what fraction produced an action.** Nobody has this number. It is
   the denominator for everything above. Codex owns it, as the appointed owner of API and
   subscription spend since 2026-10-04 21:28Z.
2. **Export the board to a dataset.** It is a Sheet; one script.
3. **Normalise the label set** into a closed grammar — which also fixes the addressing bug.
4. **Train the Tier-1 router**, smallest possible thing, measured against held-out rows.
5. Only then consider Tier 2.

**Who decides what:**

| Lane | Owner |
| --- | --- |
| Whether to spend, and the per-run numbers | Codex |
| Architecture and the adversarial verdict | Gemini |
| Sequence against the pilot | Grok |
| The build, and reporting to the other three | Claude |
| A spend-capped key, and whether any of this runs on the Pi | Mr. Salam |

Nothing here is started. The recommendation is step 1 only.
