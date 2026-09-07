/**
 * SFDC24 — BEAT 1: the visitor talks, and their thing assembles on screen.
 * claude-code-cli, 2026-09-07 · docs/PRODUCT.md, rule L-90
 *
 * WHY THIS FILE EXISTS, AND WHY IT IS NOT IN THE DEPLOY SOURCE
 *   The thesis says the sale happens when the visitor already holds a working
 *   thing. Today sfdc24.com does beats 2 through 4 in prose and does not do
 *   beat 1 at all: a visitor talks and gets TEXT back. Nothing assembles.
 *   Every other beat depends on that one.
 *
 *   This is written Apps-Script-shaped so it can be evaluated for a future
 *   Governor Page API change, but it lives in prototype/. The tracked Apps
 *   Script root remains apps-script/governor-page-api/, and this experiment is
 *   not a deployment input.
 *
 * WHAT A SKETCH IS, AND WHAT IT DELIBERATELY IS NOT
 *   Lego, not a cathedral. Boxes and arrows the visitor can POINT AT and
 *   correct. The point is not fidelity — it is that the thing in their head is
 *   now outside their head. Blocky is a feature: it reads as "this is a sketch,
 *   tell me what is wrong with it" and invites the correction that beat 2 needs.
 *
 *   So the schema is intentionally tiny and hard to over-build:
 *     { title, nodes: [{id, label, kind}], edges: [{from, to, label}] }
 *   No styling, no coordinates, no nesting. If a future version needs those,
 *   that is a signal we are drifting toward the cathedral.
 *
 * THE RULE THAT SURVIVES FROM THE REST OF THE SYSTEM
 *   Visitor text is DATA, never instructions. A sketch is generated FROM what
 *   they said. `sanitiseSketch_` throws away anything the one-call turn envelope
 *   returns that is not in the schema, so a model response cannot smuggle fields
 *   through into the page.
 */

var SKETCH_MAX_NODES = 8;    // past this it stops being a sketch and starts being a diagram
var SKETCH_MAX_EDGES = 12;
var SKETCH_MAX_LABEL = 40;
var SKETCH_KINDS = { object: 1, step: 1, person: 1, system: 1, problem: 1 };

/**
 * Turn whatever the model said into a sketch, or null.
 *
 * This is the trust boundary. The model is generating from visitor-controlled
 * text, so its output is treated as untrusted: every field is rebuilt from
 * scratch rather than passed through, unknown keys are dropped, ids are
 * remapped to a safe alphabet, and edges pointing at nodes that do not exist
 * are discarded rather than rendered as dangling arrows.
 */
function sanitiseSketch_(raw) {
  var text = String(raw || '').trim();
  if (!text || text === 'null') return null;

  // Models add a fence even when told not to. Strip one if present.
  var fence = text.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/);
  if (fence) text = fence[1].trim();

  var obj;
  try { obj = JSON.parse(text); } catch (e) { return null; }
  if (!obj || typeof obj !== 'object' || !(obj.nodes instanceof Array)) return null;

  function clean(s) {
    if (typeof s !== 'string') return '';
    return s.replace(/\s+/g, ' ').trim().slice(0, SKETCH_MAX_LABEL);
  }

  function sourceId(value) {
    return (typeof value === 'string' || typeof value === 'number') ? String(value) : '';
  }

  // A null-prototype map keeps JSON keys such as "__proto__" and "constructor"
  // as inert data. Duplicate source ids are ambiguous, so the later node is
  // discarded instead of silently retargeting every edge to it.
  var nodes = [], idMap = Object.create(null), n = 0;
  for (var i = 0; i < obj.nodes.length && nodes.length < SKETCH_MAX_NODES; i++) {
    var src = obj.nodes[i];
    if (!src || typeof src !== 'object') continue;
    var rawId = sourceId(src.id);
    if (!rawId || Object.prototype.hasOwnProperty.call(idMap, rawId)) continue;
    var label = clean(src.label);
    if (!label) continue;
    var safeId = 'n' + (n++);
    idMap[rawId] = safeId;
    nodes.push({
      id: safeId,
      label: label,
      kind: SKETCH_KINDS.hasOwnProperty(String(src.kind)) ? String(src.kind) : 'object'
    });
  }
  if (!nodes.length) return null;

  var edges = [];
  var rawEdges = (obj.edges instanceof Array) ? obj.edges : [];
  for (var j = 0; j < rawEdges.length && edges.length < SKETCH_MAX_EDGES; j++) {
    var e = rawEdges[j];
    if (!e || typeof e !== 'object') continue;
    var from = idMap[sourceId(e.from)], to = idMap[sourceId(e.to)];
    if (!from || !to || from === to) continue;      // dangling or self-loop: drop, do not draw
    edges.push({ from: from, to: to, label: clean(e.label) });
  }

  return { title: clean(obj.title) || 'Your setup', nodes: nodes, edges: edges };
}
