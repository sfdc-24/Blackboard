// The blackboard. The project's mission is "joins a live call, knows the org,
// and leaves an evidence-backed finding" — this is the leaving-a-finding half.
//
// Uses the no-follow redirect pattern REQ-PR4EXZ settled as standing law: POST
// with redirects suppressed, then a plain GET on the Location header. Following
// automatically returns a redirect artifact instead of the true JSON response.
import { config } from './config.js';

export function boardConfigured() {
  return Boolean(config.busUrl && config.busSecret);
}

/**
 * Append one row to a sheet on the board.
 * D-4 SAYS READ-BACK IS THE ONLY PROOF OF A WRITE. This returns what the bus
 * said; it verifies nothing, and the caller must never blind-retry — a retry
 * after an ambiguous failure is how duplicate rows get onto the board.
 *
 * @param {string[]} row  native array of cell values (never a joined string)
 */
export async function appendRow(title, row) {
  if (!boardConfigured()) return { ok: false, error: 'BUS_URL / BUS_SECRET not configured' };

  const payload = {
    action: 'append',
    secret: config.busSecret,
    title,
    sheetRow: row,
  };

  try {
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
    const parsed = parseBus(text);
    if (!parsed) return { ok: false, error: `hop 2 returned non-JSON (HTTP ${hop2.status})` };
    return parsed;
  } catch (err) {
    // The write may still have landed. Say so rather than implying it did not.
    return { ok: false, error: `${err.message} — the write MAY have landed; read back before retrying` };
  }
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
