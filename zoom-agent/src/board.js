// The blackboard. The project's mission is "joins a live call, knows the org,
// and leaves an evidence-backed finding" — this is the leaving-a-finding half.
//
// Uses the no-follow redirect pattern REQ-PR4EXZ settled as standing law: POST
// with redirects suppressed, then a plain GET on the Location header. Following
// automatically returns a redirect artifact instead of the true JSON response.
//
// D-4: READ-BACK IS THE ONLY PROOF OF A WRITE.
//   The previous version POSTed, read the bus's own reply, and reported the
//   finding as posted. The bus saying `ok:true` is the bus describing its own
//   intent — it is not the board. This module now appends ONCE and then reads
//   the sheet back to count the row by Row_ID, and reports what it actually
//   found.
//
// THE ONE RULE THAT MUST NEVER BE RELAXED HERE
//   An append is attempted exactly once. The googleusercontent redirect hop can
//   raise client-side AFTER the row has been written server-side, and the bus
//   does not deduplicate — so a retry writes the row twice. On 2026-09-08 that
//   put WRK-vmccc-xray-blocker-20260908T1630Z on the board twice. An exception
//   is not evidence of absence, and `ok:true` is not evidence of arrival. Only
//   the read-back counts, and when it is inconclusive this says so instead of
//   trying again.
import { config } from './config.js';

const READ_ATTEMPTS = 3;

export function boardConfigured() {
  return Boolean(config.busUrl && config.busSecret);
}

/**
 * Append one row, then read the board back and count it by Row_ID.
 *
 * @param {string}   title  sheet name
 * @param {string[]} row    native array of cell values (never a joined string)
 * @returns {{
 *   ok: boolean, verified: boolean, matches: number|null,
 *   rowId: string, ambiguous: boolean, error?: string, busSaid?: string
 * }}
 *   `verified` is true ONLY when the sheet contains this Row_ID exactly once.
 *   `ambiguous` means the outcome is genuinely unknown and a human must look —
 *   it is never a signal to retry.
 */
export async function appendRow(title, row) {
  const rowId = String(row?.[0] ?? '');
  const fail = (error, extra = {}) => ({
    ok: false, verified: false, matches: null, rowId, ambiguous: false, error, ...extra,
  });

  if (!boardConfigured()) return fail('BUS_URL / BUS_SECRET not configured');
  if (!rowId) return fail('row[0] must be a Row_ID — without one the write cannot be verified');

  let busSaid = '';
  let transportError = null;

  try {
    const posted = await postOnce({
      action: 'append',
      secret: config.busSecret,
      title,
      sheetRow: row,
    });
    busSaid = posted.ok ? 'bus reported ok' : `bus reported: ${posted.error}`;
  } catch (err) {
    // Do NOT return here. The write may well have landed; the read-back below
    // is the only thing that can tell us, and retrying the POST is the one
    // action guaranteed to make things worse.
    transportError = err.message;
    busSaid = `transport error: ${err.message}`;
  }

  const seen = await countRowId(title, rowId);

  if (seen.error) {
    return {
      ok: false, verified: false, matches: null, rowId, busSaid,
      ambiguous: true,
      error: `could not read back (${seen.error}) — the row MAY be on the board. `
        + 'Check by Row_ID before writing anything else. Do not retry the append.',
    };
  }
  if (seen.count === 1) {
    return { ok: true, verified: true, matches: 1, rowId, ambiguous: false, busSaid };
  }
  if (seen.count > 1) {
    return {
      ok: false, verified: false, matches: seen.count, rowId, busSaid,
      ambiguous: false,
      error: `Row_ID ${rowId} appears ${seen.count} times — a duplicate is already on the board`,
    };
  }
  return {
    ok: false, verified: false, matches: 0, rowId, busSaid,
    ambiguous: true,
    error: transportError
      ? `not found on read-back after a transport error (${transportError}). A read can also be `
        + 'stale, so absence is not proof either. Check by Row_ID before retrying anything.'
      : 'not found on read-back. A successful read can omit a row appended moments earlier '
        + '(CLAUDE-BOARD-STALE-READ-FINDING-001), so this is unresolved rather than failed.',
  };
}

/** One POST, one hop, no retries. See the header comment for why. */
async function postOnce(payload) {
  const hop1 = await fetch(config.busUrl, {
    method: 'POST',
    redirect: 'manual',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  // Apps Script answers a POST with a 302 to a one-shot result URL.
  const location = hop1.headers.get('location');
  if (!location) {
    const direct = await hop1.text().catch(() => '');
    return parseBus(direct) ?? { ok: false, error: `no redirect and no JSON (HTTP ${hop1.status})` };
  }
  const hop2 = await fetch(location, { redirect: 'follow' });
  const text = await hop2.text();
  return parseBus(text) ?? { ok: false, error: `hop 2 returned non-JSON (HTTP ${hop2.status})` };
}

/**
 * Count how many rows carry this Row_ID.
 *
 * Reads are idempotent, so unlike the append these may be retried freely — the
 * redirect target intermittently 404s and the bus sometimes answers a read with
 * a small health blob instead of the board.
 */
async function countRowId(title, rowId) {
  let last = 'no attempt made';
  let sawValidRead = false;

  for (let i = 0; i < READ_ATTEMPTS; i += 1) {
    try {
      const body = await postOnce({ action: 'read', secret: config.busSecret, title });
      const rows = body?.rows;
      if (!Array.isArray(rows)) {
        last = 'response carried no rows array';
        continue;
      }
      sawValidRead = true;
      const count = rows.filter((r) => String(r?.[0] ?? '') === rowId).length;
      // Found it (or found it twice): settled, return immediately.
      if (count > 0) return { count };
      // NOT found. This module already knows a successful read can omit a row
      // appended moments earlier, so a single absent read is not an answer --
      // it is the one case worth spending another idempotent attempt on.
      // Retrying only malformed responses meant the exact condition the header
      // comment describes was the one condition never retried.
      last = 'row absent from an otherwise valid read';
    } catch (err) {
      last = err.message;
    }
  }
  // Every attempt spent. If at least one read was structurally valid we can say
  // the row is absent from the board as last seen; if none were, we know nothing.
  return sawValidRead ? { count: 0 } : { error: last };
}

function parseBus(text) {
  const t = String(text ?? '').trim();
  if (!t.startsWith('{')) return null;
  try { return JSON.parse(t); } catch { return null; }
}

/** A board row in the fleet's BCB grammar. */
export function bcb(fields) {
  return 'BCB|v=1|' + Object.entries(fields)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}=${String(v).replace(/[|\r\n]+/g, ' ')}`)
    .join('|');
}
