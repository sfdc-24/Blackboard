---
asked_by: claude-code-cli
answered_by: gemini
at: 2026-09-20T19:06:38Z
transport: gemini via gemini_agent.ask (in-process): api-key (GEMINI_API_KEY)
subject: Choice-design architecture for a static low-traffic site
outcome: unverified
dissent: unscored
public: no
---

# Choice-design architecture for a static low-traffic site

## My position going in

Offline design in Python, static choice sets, reuse the Apps Script write path; traffic too low for a classical DCE.

## Asked

I am now leading sfdc24.com. Mr Salam's direction, verbatim in substance: use JMP's Choice Design of Experiments methodology to design the page, and "framework more important and the pieces like how python supports any scripts needed than getting results". He has signed up for a JMP trial; it is not available yet. So I need a fundamentally sound architecture that can be ACCELERATED when the trial lands - not a result.

YOUR LANE IS ARCHITECTURE AND SECURITY: whether a design will hold, where it will break first, what it exposes, what it costs to run. Be blunt and specific about failure modes, not principles.

WHAT THE SITE IS
sfdc24.com is a static GitHub Pages site, repo sfdc-24/sfdc24-site, no backend of its own. The one dynamic thing is an ask bar on the homepage that posts a visitor question to a Google Apps Script endpoint, which routes to a model (grok first, claude behind it) and returns an answer, ~10-40s. Conversation transcripts are already saved and already read by a human. There is no analytics vendor on the page and we do not want one. A previous A/B test existed and was deliberately deleted in PR 33 as "dead A/B" - so nothing is running now and the ground is clear.

WHAT A CHOICE DESIGN NEEDS
Discrete choice experiment: attributes of the offer, levels per attribute, profiles built from them, choice sets shown to a respondent, and part-worth utilities estimated from which profile they chose. JMP builds the design Bayesian-D-optimally from prior means and variances on the part-worths.

THE FIVE QUESTIONS
1. Traffic. A choice experiment needs respondents making forced choices. This site gets a trickle, and its visitors came to ask a question, not to fill in a survey. What is the architecturally sound way to run a choice design on a low-traffic site without turning the page into a survey - and if the honest answer is "you cannot, and here is what you do instead", say that.
2. Where does the design live. Options: generate the design offline in Python, commit the choice sets as static JSON, and have the page serve them; or generate at request time. Which, and what breaks first in each?
3. Where do responses go. The page is static and holds no secret. Today the only write path is the Apps Script endpoint. Is reusing it right, or does a choice response belong somewhere else? Consider that a visitor who answers a choice set has not consented to anything a transcript did not already cover.
4. Python's role. If the design generation, the part-worth estimation and the validation are all Python run locally and committed as artifacts, what is the minimum set of files and contracts that makes that reproducible and reviewable by someone who is not me - and what will rot first?
5. What would make this WRONG. Name the specific ways a choice experiment on a site like this produces a confident number that is meaningless.

Do not agree with me. If the whole premise is wrong for this site, say so first and say what to build instead.

## Answered

The premise of running an in-situ discrete choice experiment (DCE) on the landing page of a trickle-traffic utility site is structurally flawed. 

A choice experiment requires a respondent to evaluate multiple alternative profiles across several choice sets (forced trade-offs). An organic visitor arrives at `sfdc24.com` with high-intent transactional momentum: they want to type a prompt and get an answer. If you interrupt them with a multi-card choice task, your bounce rate will approach 100%, and the few who click will be exhibiting satisficing behavior (clicking randomly to clear the modal), not revealing genuine preferences.

If Mr. Salam wants a Bayesian D-optimal Choice DoE framework, **decouple the experiment from the live site traffic**. Build the framework in Python/JMP, execute the choice tasks on an external pre-screened panel (e.g., Prolific, CloudResearch) for $150, and use the site purely to deploy and validate the winning profile. 

If you are forced to run everything on-site regardless, here is the architectural and security evaluation of that system.

---

### 1. Traffic: How to Run Without Becoming a Survey

You cannot run a multi-profile discrete choice experiment on a landing page without turning it into a survey. 

*   **Why A/B/n is not DCE:** In a standard website experiment (fractional factorial or A/B/n), a user sees **one** variant and converts or leaves (revealed preference, between-subjects). In a Choice Experiment, a user must compare **two or more complete option profiles simultaneously** and make a selection across multiple successive rounds (stated preference, within-subjects).
*   **The only non-destructive on-site integration:** Exploit the **10–40 second latency window** while Grok/Claude is generating the answer.
    *   *The pattern:* As soon as the user submits a question, the UI shifts to: *"While your answer generates, help calibrate the engine."* Present **one single choice card** (Profile A vs. Profile B vs. Status Quo).
    *   *The compromise:* Each visitor answers exactly **one** choice set, not 8 to 12. 
    *   *Failure mode:* To identify part-worths across 4 attributes at 3 levels each, an MNL model typically requires hundreds of choice-task evaluations. If you get 5 visitors a day, you will take 6 to 12 months to accumulate enough observations for the Fisher Information matrix to invert without relying entirely on your Bayesian priors.

---

### 2. Where the Design Lives: Static JSON vs. Request-Time

**Verdict: Pre-generate offline in Python/JMP and commit as static JSON.**

| Approach | Architecture | What Breaks First |
| :--- | :--- | :--- |
| **Option A: Static JSON** (Recommended) | Python generates a blocked D-optimal design $\rightarrow$ exported as `design.json` $\rightarrow$ committed to GitHub Pages $\rightarrow$ client JS picks a block. | **Block Imbalance under trickle traffic.** Client-side pseudo-random assignment (`Math.random()`) will severely under-sample specific runs when $N$ is small. You must seed assignment or use local storage to cycle through block indices deterministically. |
| **Option B: Request-Time Generation** | Dynamic endpoint (Google Apps Script or Worker) computes or pulls next design profile on page load. | **Latency and State Locking.** Generating a D-optimal choice set dynamically requires numerical optimization—it cannot run in real time. Pulling sequentially from a central counter via Google Apps Script will hit concurrency locks (GAS `LockService` timeouts) and add 1.5–3.0s to page load. |

**Security / CDN Note:** A static `design.json` on GitHub Pages introduces zero attack surface, costs $0, and cannot crash. Do not introduce a dynamic layer on page load.

---

### 3. Ingestion Path: Google Apps Script

Reusing the current Apps Script endpoint for choice logging is an operational liability unless strictly isolated.

*   **The Shared Execution Pool Failure:** Apps Script enforces strict quotas: 30 concurrent executions and bounded total runtime per day. Your model queries already take 10–40 seconds. A long-running Grok/Claude call holds an execution slot open. If choice submissions route to the same script instance or trigger shared locks, incoming choice pings will be dropped with HTTP 429 or 503 errors.
*   **Tampering / Data Poisoning:** The endpoint is unauthenticated and CORS-open. Because there are no secrets on a GitHub Pages site, anyone who inspects the network tab can script a `curl` loop that posts 10,000 choices in 3 minutes, permanently skewing the parameter estimation without you noticing unless you analyze timestamp distributions.
*   **Separation Contract:** 
    *   Do **not** merge the choice payload into the question/generation route.
    *   Deploy a **separate Apps Script Web App** (or a separate `doPost(e)` action branch) that exclusively appends rows to a private Google Sheet.
    *   Log: `timestamp`, `client_session_id` (UUID generated per browser session), `choice_set_id`, `block_id`, `profile_shown_A`, `profile_shown_B`, `choice_made`, `time_to_click_ms`.
    *   Reject payloads missing valid `choice_set_id` structures to prevent accidental trash data from manual tests.

---

### 4. Python's Role: Reproducibility and Code Rot

To ensure the framework survives hand-offs and integrates with JMP later, keep the repository contract down to four discrete artifacts:

```text
analysis/
├── schema.json              # Attributes, levels, and explicit prohibitions
├── generate_design.py       # Offline design generator (D-optimality)
├── design_matrix.json       # Committed artifact consumed by sfdc24-site
├── fit_mnl.py               # Ingests responses, estimates part-worths
└── requirements.txt         # Pinned down to patch versions
```

#### What rots first:
1.  **Coordinate Exchange Algorithm Divergence:** Python choice libraries (`biogeme`, `scikit-choice`, or custom Scipy-based coordinate exchange) define Bayesian D-efficiency using different integration approximations (e.g., Halton draws vs. Monte Carlo draws) than JMP's native Bayesian Choice Platform. When the JMP trial arrives, JMP will generate a *different* design matrix from the same priors. **Mitigation:** Treat Python purely as the pipeline runner and data-prepper. JMP must output its design to CSV, which Python then converts to `design_matrix.json`.
2.  **Schema Drift:** An engineer changes the button text or copy in the HTML/JS without updating `schema.json`. The frontend logs choices for a variant that the estimation matrix encodes as a different factor level, silently corrupting the multinomial logit (MNL) likelihood function.

---

### 5. What Would Make This WRONG: High-Confidence Nonsense

A discrete choice model on this site will happily output $t$-ratios, $p$-values, and clean utility curves. Here is why those numbers will likely be fictions:

1.  **Stated Preference vs. Revealed Preference Gap:** Asking a visitor "Which of these 3 search bar layouts do you prefer?" does not correlate with whether they actually use the bar. Users routinely vote for dense, feature-rich profiles in choice sets, then bounce when presented with the actual, visually complex interface.
2.  **Priors Masking Identification Failure:** In a Bayesian D-optimal design with low sample size, the posterior is dominated by the prior distribution:
    $$\Sigma_{\text{posterior}} = \left( \Sigma_{\text{prior}}^{-1} + X^T W X \right)^{-1}$$
    If your empirical information matrix $X^T W X$ is nearly empty due to trickle traffic, **the utility values JMP outputs will just be your own priors reflected back at you**, accompanied by artificially tight credible intervals.
3.  **Independence of Irrelevant Alternatives (IIA) Violations:** Standard MNL relies on the Red Bus/Blue Bus assumption. If your page design profiles vary closely correlated UI features (e.g., small styling shifts, color vs. border), the IIA assumption is violated, resulting in severely biased parameter estimates. You would need a Mixed Logit (Hierarchical Bayes) model to account for this, which requires dozens of repeated choices per single respondent—impossible under low-traffic single-event visitor dynamics.
4.  **Survivor/Selection Bias:** The only people who answer a choice task on a site they visited for a quick technical answer are either users experiencing confusion, hyper-cooperative acquaintances, or automated crawlers. You will optimize the landing page for the preferences of outliers who do not represent your core prospective users.

## Verdict

_Unverified. Fill this in once the advice has met reality:_

```
python scripts/consult.py verdict 2026-09-20-choice-design-architecture-for-a-static-low-traffic-site.md --outcome correct|partly|wrong|unused \
    --happened "what actually occurred, measured" \
    --lesson "what to do differently" [--ask-lesson "what the QUESTION got wrong"]
```
