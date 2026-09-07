/**
 * SFDC24 — BEAT 3: turn the corrected sketch into something they can hold.
 * claude-code-cli, 2026-09-07 · docs/PRODUCT.md
 *
 * WHAT "THEIR THING" IS, FOR THIS BUSINESS
 *   Mr. Salam asked for websites on demand. The literal reading — a marketing
 *   site — is the wrong product for someone arriving with a broken Salesforce
 *   process. What they actually cannot get anywhere else is a clear, sharable
 *   picture of what their own process does and where it breaks, with the first
 *   move named. That is a page, it is generated on demand, and it is worth
 *   sending to their boss. It is also the only artefact we can produce honestly
 *   without touching their org.
 *
 *   So beat 3 v1 renders exactly that: their sketch, the break called out, and
 *   what to do first. If they want it hosted, it is a static file and a
 *   subdomain — no registrar arrangement needed (DOMAIN-ONDEMAND-001).
 *
 * WHY THIS FILE IS DETERMINISTIC
 *   Everything model-shaped happens before it. This takes a sketch and a few
 *   short strings and returns HTML — no network, no key, no randomness. That
 *   makes it testable without a provider, and it means a bad page is a bug in
 *   code rather than a bad day from a model.
 *
 * EVERYTHING IS ESCAPED, WITHOUT EXCEPTION
 *   Every string here traces back to visitor-controlled text. The sketch was
 *   already rebuilt field-by-field by sanitiseSketch_, but this file is the last
 *   thing standing between that text and a file someone opens in a browser and
 *   may host on our domain. So it escapes on the way out too. Two boundaries
 *   for the same text is not redundancy; it is the only reason a mistake in one
 *   of them is survivable.
 */

var KIND_FILL = {
  object:  { fill: '#ffffff', stroke: '#0B0D10' },
  step:    { fill: '#ffffff', stroke: '#0B0D10' },
  person:  { fill: '#F2F7FF', stroke: '#0B0D10' },
  system:  { fill: '#F6F3FF', stroke: '#0B0D10' },
  problem: { fill: '#FFF4F2', stroke: '#C2410C' }
};

function esc_(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Lay the sketch out and return SVG. Same column-walk as the live stage: eight
 * boxes do not need a layout engine, and reaching for one is the first step
 * toward the cathedral PRODUCT.md warns about.
 */
function sketchSvg_(sketch) {
  var N = sketch.nodes, E = sketch.edges || [];
  var depth = {};
  N.forEach(function (n) { depth[n.id] = 0; });
  for (var p = 0; p < N.length; p++) {
    E.forEach(function (e) {
      if (depth[e.to] !== undefined && depth[e.from] !== undefined && depth[e.to] <= depth[e.from]) {
        depth[e.to] = depth[e.from] + 1;
      }
    });
  }
  var cols = {};
  N.forEach(function (n) { (cols[depth[n.id]] = cols[depth[n.id]] || []).push(n); });
  var keys = Object.keys(cols).sort(function (a, b) { return a - b; });

  var W = 168, H = 54, GX = 54, GY = 24, PAD = 14, pos = {};
  keys.forEach(function (k, ci) {
    cols[k].forEach(function (n, ri) { pos[n.id] = { x: PAD + ci * (W + GX), y: PAD + ri * (H + GY) }; });
  });
  var rows = Math.max.apply(null, keys.map(function (k) { return cols[k].length; }));
  var w = PAD * 2 + keys.length * (W + GX) - GX;
  var h = PAD * 2 + rows * (H + GY) - GY;

  var out = ['<svg viewBox="0 0 ' + w + ' ' + h + '" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="' + esc_(sketch.title) + '">'];
  E.forEach(function (e) {
    var a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    var x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, m = (x1 + x2) / 2;
    out.push('<path d="M' + x1 + ',' + y1 + ' C' + m + ',' + y1 + ' ' + m + ',' + y2 + ' ' + (x2 - 8) + ',' + y2 +
             '" fill="none" stroke="#0B0D10" stroke-width="2"/>');
    out.push('<path d="M' + (x2 - 9) + ',' + (y2 - 5) + ' L' + x2 + ',' + y2 + ' L' + (x2 - 9) + ',' + (y2 + 5) +
             '" fill="none" stroke="#0B0D10" stroke-width="2"/>');
    if (e.label) {
      out.push('<text x="' + m + '" y="' + ((y1 + y2) / 2 - 7) + '" text-anchor="middle" font-family="ui-monospace,monospace" font-size="11" fill="#6B7280">' + esc_(e.label) + '</text>');
    }
  });
  N.forEach(function (n) {
    var q = pos[n.id], c = KIND_FILL[n.kind] || KIND_FILL.object;
    out.push('<g><rect x="' + q.x + '" y="' + q.y + '" width="' + W + '" height="' + H + '" rx="8" fill="' + c.fill + '" stroke="' + c.stroke + '" stroke-width="2"/>');
    out.push('<text x="' + (q.x + 12) + '" y="' + (q.y + 20) + '" font-family="ui-monospace,monospace" font-size="9" fill="#6B7280" letter-spacing="1">' + esc_(String(n.kind).toUpperCase()) + '</text>');
    out.push('<text x="' + (q.x + 12) + '" y="' + (q.y + 39) + '" font-family="system-ui,sans-serif" font-size="14" font-weight="600" fill="#0B0D10">' + esc_(n.label) + '</text></g>');
  });
  out.push('</svg>');
  return out.join('');
}

/**
 * Render the whole page. `parts` carries the short human strings:
 *   { headline, whatsHappening, whereItBreaks, firstMove }
 * Missing parts are omitted rather than filled with filler — an empty section
 * that says nothing is worse than no section, and inventing content here is how
 * the thing stops being theirs.
 */
function buildPage_(sketch, parts, stamp) {
  parts = parts || {};
  var title = (sketch && sketch.title) || 'Your process';
  var blocks = [];

  function section(h, body) {
    if (!body) return;
    blocks.push('<section><h2>' + esc_(h) + '</h2><p>' + esc_(body) + '</p></section>');
  }
  section('What is happening', parts.whatsHappening);
  section('Where it breaks', parts.whereItBreaks);
  section('What to do first', parts.firstMove);

  return [
'<!DOCTYPE html>',
'<html lang="en"><head><meta charset="utf-8">',
'<meta name="viewport" content="width=device-width,initial-scale=1">',
"<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; object-src 'none'; base-uri 'none'; form-action 'none'\">",
'<title>' + esc_(title) + '</title>',
'<meta name="robots" content="noindex,nofollow">',
// Self-contained on purpose: one file, no fonts, no CDN, no scripts. They will
// email it, and a page that needs the network to render is a page that arrives
// broken on someone else's laptop.
'<style>',
'*{box-sizing:border-box}',
'body{margin:0;background:#FBFAF7;color:#0B0D10;font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif}',
'main{max-width:820px;margin:0 auto;padding:40px 22px 64px}',
'.eyebrow{font:600 11px/1 ui-monospace,monospace;letter-spacing:.13em;text-transform:uppercase;color:#6B7280}',
'h1{font-size:29px;line-height:1.2;margin:8px 0 6px}',
'.lede{color:#374151;margin:0 0 26px;font-size:17px}',
'figure{margin:0 0 30px;padding:18px;background:#F3EFE7;border:1px solid #E3DED2;border-radius:12px;overflow-x:auto}',
'figure svg{max-width:100%;height:auto;display:block}',
'h2{font-size:13px;letter-spacing:.09em;text-transform:uppercase;color:#6B7280;margin:26px 0 5px}',
'section p{margin:0}',
'footer{margin-top:38px;padding-top:16px;border-top:1px solid #E7E3DA;color:#6B7280;font-size:14px}',
'footer a{color:#0B0D10}',
'@media print{body{background:#fff}figure{background:#fff}}',
'</style></head><body><main>',
'<p class="eyebrow">Sketched from your own description</p>',
'<h1>' + esc_(title) + '</h1>',
parts.headline ? '<p class="lede">' + esc_(parts.headline) + '</p>' : '',
'<figure>' + (sketch && sketch.nodes && sketch.nodes.length ? sketchSvg_(sketch) : '') + '</figure>',
blocks.join(''),
'<footer>',
// Says plainly what it is and is not. A generated page that implies an audit
// took place would be the quote-comparison mode PRODUCT.md exists to avoid,
// and it would be a lie about how it was made.
'<p>This came out of one conversation on sfdc24.com &mdash; nobody looked inside your Salesforce org to make it. ',
'It is a starting point to correct, not an audit. If a box is wrong, that is the useful part: say so and it changes.</p>',
'<p>' + esc_(stamp || '') + ' &middot; <a href="mailto:abdus@sfdc24.com">abdus@sfdc24.com</a></p>',
'</footer></main></body></html>'
  ].filter(Boolean).join('\n');
}
