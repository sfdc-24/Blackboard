// Fitting a call into the contract the backend actually honours.
//
// THE DEFECT THIS EXISTS TO REMOVE
//   reception() truncates the query server-side:
//
//     var CHAT_MAX_INPUT = 1000;                 // Code.gs:34
//     var text = String(p.q || '').slice(0, CHAT_MAX_INPUT);   // Code.gs:196
//
//   The agent was sending up to 400 transcript lines in one `q`. A real
//   hour-long call is tens of thousands of characters, so what arrived was the
//   first ~1,000 — the opening pleasantries — and the model then wrote a
//   confident "summary of the call" from the greeting. Nothing failed. No error
//   was raised. The output looked exactly like a working summary, which is the
//   worst failure shape there is.
//
//   Truncation is only safe when someone decides WHAT to drop. Here that
//   decision is made explicitly, in one place, and reported to the caller so it
//   can be disclosed rather than hidden.
//
// SPEND IS A REAL CONSTRAINT, NOT A DETAIL
//   Chunking an hour of speech into 1,000-char requests is dozens of backend
//   calls, each of which costs money and shares the fleet's caps. So coverage is
//   bounded by chunk count, and whatever that leaves out is COUNTED and handed
//   back, never silently dropped.

/**
 * Mirrors CHAT_MAX_INPUT in apps-script/governor-page-api/Code.gs:34.
 * If that constant changes server-side this one must change with it — which is
 * why the number appears here once and is imported everywhere else.
 */
export const MAX_QUERY_CHARS = 1000;

/**
 * Assemble a prompt that is guaranteed to fit, by dropping the OLDEST context
 * lines first. Recent speech is what a live question is about; the start of the
 * call is the part you can afford to lose.
 *
 * @param {string[]} header    instruction lines, always kept in full
 * @param {string[]} body      transcript lines, newest last, trimmed from the front
 * @param {number}   max       character budget
 * @returns {{ text: string, used: number, dropped: number }}
 */
export function buildBounded(header, body, max = MAX_QUERY_CHARS) {
  const headText = header.join('\n');
  // +1 for the newline that will join header to body.
  const room = max - headText.length - 1;
  if (room <= 0) {
    // The instructions alone do not fit. Truncating THEM would change what was
    // asked, so this is a caller error and says so rather than guessing.
    return { text: headText.slice(0, max), used: 0, dropped: body.length };
  }

  const kept = [];
  let size = 0;
  for (let i = body.length - 1; i >= 0; i -= 1) {
    const line = body[i];
    const cost = line.length + (kept.length ? 1 : 0);
    if (size + cost > room) break;
    kept.unshift(line);
    size += cost;
  }
  return {
    text: kept.length ? `${headText}\n${kept.join('\n')}` : headText,
    used: kept.length,
    dropped: body.length - kept.length,
  };
}

/**
 * Split a whole call into prompts that each fit the contract, newest first, up
 * to a bounded number of them.
 *
 * Coverage runs BACKWARDS from the end of the call and the returned chunks are
 * then put back in chronological order. When the call is longer than the budget
 * allows, what is lost is the beginning — and `dropped` says exactly how many
 * lines that was, so the summary can admit it.
 *
 * Each chunk carries the NUMBER OF LINES it represents, so the caller can
 * report coverage from the segments whose summaries actually survived — a
 * segment whose request failed, or whose note was dropped during reduction,
 * covers nothing regardless of how many lines went into it.
 *
 * @returns {{ chunks: {text: string, lines: number}[], covered: number, dropped: number }}
 */
export function planSummaryChunks(header, body, { max = MAX_QUERY_CHARS, maxChunks = 8 } = {}) {
  const headText = header.join('\n');
  const room = max - headText.length - 1;
  if (room <= 0 || body.length === 0) {
    return { chunks: [], covered: 0, dropped: body.length };
  }

  const groups = [];
  let current = [];
  let size = 0;
  // Lines that were placed only in part. They are neither covered nor dropped:
  // the tail is in the prompt, the opening is gone. Counting one as covered
  // told the summary it had summarised a line whose beginning it never saw.
  let partial = 0;

  for (let i = body.length - 1; i >= 0; i -= 1) {
    const line = body[i];
    const cost = line.length + (current.length ? 1 : 0);
    if (size + cost > room) {
      if (current.length) groups.push({ lines: current, covered: current.length });
      if (groups.length >= maxChunks) {
        current = [];
        break;
      }
      current = [];
      size = 0;
      // A single line longer than the whole budget cannot be placed whole.
      // Take the tail of it — the end of a sentence carries more than its
      // opening — but credit NOTHING for it: only part of that line reached the
      // model, and coverage is a claim about what was actually summarised.
      //
      // The comment that used to sit here said the opposite ("count it as
      // covered, because it is") and survived the fix that changed the code
      // below to credit 0. A comment that contradicts its own code is worse
      // than no comment: the next reader trusts it and reverts the fix.
      if (line.length > room) {
        groups.push({ lines: [line.slice(line.length - room)], covered: 0 });
        partial += 1;
        if (groups.length >= maxChunks) break;
        continue;
      }
    }
    current.unshift(line);
    size += current.length === 1 ? line.length : cost;
  }
  if (current.length && groups.length < maxChunks) {
    groups.push({ lines: current, covered: current.length });
  }

  const ordered = groups.slice(0, maxChunks).reverse();
  const covered = ordered.reduce((n, g) => n + g.covered, 0);
  const partialPlaced = ordered.reduce((n, g) => n + (g.covered === 0 ? g.lines.length : 0), 0);
  return {
    chunks: ordered.map((g) => ({ text: `${headText}\n${g.lines.join('\n')}`, lines: g.covered })),
    covered,
    partial: partialPlaced,
    dropped: body.length - covered - partialPlaced,
  };
}

/** True when a prompt honours the backend's contract. Used by the tests. */
export function fitsBudget(text, max = MAX_QUERY_CHARS) {
  return String(text).length <= max;
}
