/**
 * SFDC24 — SITE INTERVIEW
 * claude-code-cli, 2026-09-03. Additions for the Governor Page project.
 *
 * THE IDEA
 *   The site's pages are empty because filling them means writing copy, and
 *   writing copy is the thing that never gets done. So don't write it — answer
 *   questions about it. Reception interviews the Governor one question at a
 *   time, stores each answer on the board, and the pages render from those
 *   rows. The site fills itself as a by-product of a conversation.
 *
 *   This is the same shape the rest of the system already uses: the board is
 *   the source of truth, pages are projections. Nothing new to learn.
 *
 * WHY AN INTERVIEW BEATS A FORM
 *   A form accepts "We deliver tailored solutions." An interviewer doesn't.
 *   The pushback below is the whole point — one round of "that's marketing
 *   copy, give me the actual thing" produces text worth putting on a page.
 *
 * SECURITY
 *   Writes are GOVERNOR ONLY. A visitor must never be able to author site
 *   copy — that would be an open CMS on a public URL. Reuses the existing
 *   governor check; if that ever loosens, this must not.
 */

var SITE_PREFIX = 'SITE';   // board payloads: SITE|field=...|value=...

/**
 * The interview. Order is deliberate: the questions whose absence hurts most
 * come first, so a five-minute conversation is still worth having.
 *
 * `probe` is what the interviewer says when the answer is vague. It exists
 * because the first answer to any of these is almost always a slogan.
 */
var SITE_FIELDS = [
  { id: 'team.bio', page: 'Team',
    q: "In one paragraph, who are you? Years in the Salesforce ecosystem, the kind of orgs you've worked in, and the one thing you're unusually good at.",
    probe: "That reads like a bio anyone could have. What's the thing you're actually better at than the next consultant? Plain words." },

  { id: 'faq.data', page: 'FAQ',
    q: "When a client asks 'do your AI agents see my Salesforce data?' — what is the true answer today?",
    probe: "Be precise here. This is the question a regulated buyer stops reading over. What exactly can an agent reach, and what can it not?" },

  { id: 'faq.price', page: 'FAQ',
    q: "What does an engagement cost? A range is fine — a range converts better than 'contact us'.",
    probe: "Even a rough band beats nothing. What did the last two pieces of work actually cost?" },

  { id: 'faq.speed', page: 'FAQ',
    q: "Realistic turnaround for a typical first piece of work — not the best case.",
    probe: "Best cases get quoted back to you later. What's the number you'd be comfortable being held to?" },

  { id: 'home.proof', page: 'Home',
    q: "One real engagement: the problem in the client's words, what you did, and what changed. Say 'none' if there's nothing you can publish yet.",
    probe: "What changed for them, specifically? A number if you have one, plain language if not." },

  { id: 'contact.direct', page: 'Home',
    q: "Besides abdus@sfdc24.com, how should someone reach you? Phone, WhatsApp, booking link — or 'email only'.",
    probe: null },

  { id: 'projects.now', page: 'Projects',
    q: "What are you actually working on right now that you'd be happy for a stranger to see?",
    probe: "Concrete enough to be checkable, please — 'a WhatsApp gateway routing four models' rather than 'AI automation'." }
];

/** Newest answer wins. The board is append-only, so re-answering is just
 *  another row — no edit, no delete, full history of what the site used to say. */
function siteValues_() {
  var out = {}, rows = readRows_();
  for (var i = 0; i < rows.length; i++) {
    var p = String(rows[i][5] || '');
    if (p.indexOf(SITE_PREFIX + '|') !== 0) continue;
    var f = /\|field=([^|]+)/.exec(p), v = /\|value=([\s\S]*)$/.exec(p);
    if (f && v) out[f[1].trim()] = v[1].trim();
  }
  return out;
}

/** Render helper for the page templates. Returns '' for anything unanswered —
 *  never a placeholder. A visitor must never see [[FILL]] or "TBD"; an absent
 *  section reads as deliberate, a placeholder reads as abandoned. */
function siteText_(fieldId) {
  var v = siteValues_()[fieldId];
  return (v && v.toLowerCase() !== 'none') ? v : '';
}

/** What is still missing, in priority order. */
function siteGaps_() {
  var have = siteValues_(), gaps = [];
  for (var i = 0; i < SITE_FIELDS.length; i++) {
    if (!have[SITE_FIELDS[i].id]) gaps.push(SITE_FIELDS[i]);
  }
  return gaps;
}

/**
 * Store one answer. GOVERNOR ONLY.
 *
 * Newlines are flattened because the board is one row per fact and a payload
 * with raw newlines breaks every downstream grep. Value is capped so a runaway
 * answer cannot blow the cell limit.
 */
function siteAnswer_(fieldId, value) {
  if (!isGovernor_()) return { ok: false, error: 'Not authorised.' };

  var known = false;
  for (var i = 0; i < SITE_FIELDS.length; i++) if (SITE_FIELDS[i].id === fieldId) known = true;
  if (!known) return { ok: false, error: 'Unknown field: ' + fieldId };

  var clean = String(value || '').replace(/[\r\n]+/g, ' ').trim().slice(0, 3000);
  if (!clean) return { ok: false, error: 'Empty answer.' };

  var res = appendRow_(SITE_PREFIX + '|field=' + fieldId + '|value=' + clean,
                       'governor-page', 'site-interview');
  // D-4: the append is not proof. Read it back before telling anyone it stuck.
  var landed = siteValues_()[fieldId] === clean;
  return { ok: landed, wrote: res, verified: landed,
           remaining: siteGaps_().length };
}

/**
 * Replace isGovernor_ with whatever the project already uses — this project
 * checks the signed-in address against GOVERNOR_EMAILS, and separately accepts
 * GOVERNOR_PASS on doPost. Wire to that; do NOT invent a second auth path.
 */
function isGovernor_() {
  var props = PropertiesService.getScriptProperties();
  var allow = String(props.getProperty('GOVERNOR_EMAILS') || '')
                .split(',').map(function (s) { return s.trim().toLowerCase(); })
                .filter(String);
  var me = '';
  try { me = String(Session.getActiveUser().getEmail() || '').toLowerCase(); } catch (e) {}
  if (!me) return false;
  if (!allow.length) return me === String(Session.getEffectiveUser().getEmail() || '').toLowerCase();
  return allow.indexOf(me) !== -1;
}

/**
 * Interviewer system prompt. Appended to the normal reception prompt when the
 * Governor is signed in and gaps remain.
 */
function INTERVIEW_PROMPT_(field, priorAnswer) {
  return [
    "INTERVIEW MODE. You are collecting one specific answer for the SFDC24 website from Mr. Salam himself, not from a visitor.",
    "",
    "The field you are filling: " + field.id + " (appears on the " + field.page + " page).",
    "Ask exactly this, in your own voice, and nothing else: " + field.q,
    "",
    "RULES",
    "- One question at a time. Never stack two.",
    "- When he answers, judge it as copy a stranger will read. If it is vague, a slogan, or something any consultancy could say, push back ONCE and then accept whatever comes back. Two rounds of pushback is nagging.",
    (field.probe ? "- Your one pushback, adapted to what he actually said: " + field.probe : "- No pushback needed for this field; take the answer as given."),
    "- Never write the copy for him. Suggesting words produces a site that sounds like you instead of him, which is the one thing this exercise is for.",
    "- Accept 'skip' or 'none' immediately and move on without comment.",
    "- When the answer is good enough, say only that it is stored and name what is next. No praise.",
    "",
    priorAnswer ? "His answer so far: " + priorAnswer : ""
  ].join('\n');
}

/**
 * WIRING (three small edits in Code.gs)
 *
 * 1. In doGet, add a view:
 *      if (p.view === 'interview' && isGovernor_()) return page_('Interview');
 *
 * 2. In reception(), when the Governor is signed in and siteGaps_() is not
 *    empty, append INTERVIEW_PROMPT_(siteGaps_()[0], null) to the system
 *    prompt and let the normal chat loop run. No separate UI needed — the
 *    interview IS the chat.
 *
 * 3. In the page templates, replace each hardcoded gap with siteText_():
 *      <?= siteText_('team.bio') ?>
 *    and wrap the section so it renders nothing when the value is ''.
 *
 * Deploy a NEW VERSION afterwards (DEPLOY.md) — code changes do not go live
 * on save.
 */
