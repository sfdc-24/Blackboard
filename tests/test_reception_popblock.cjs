/*
  The "open this address directly" block on Reception.html.

  WHAT IS ON TRIAL
    The block used to be built under `if (!document.getElementById("popblock"))`,
    so it was created on the first click and never touched again. Click the voice
    link, then Sign out, and the page still offered the VOICE address under
    "open this address directly" - a confidently wrong address shown to a
    visitor, which is worse than showing none.

    The guard predates PR33. It had never RENDERED before, because esc() threw
    first; fixing that crash promoted a latent bug into a live wrong answer.
    Found by vm-claude-code-cli reviewing 77ec7e3.

  WHY A HAND-BUILT DOM IS ACCEPTABLE HERE
    This repo has no package.json, no jsdom and no browser runner, and adding one
    for a single Apps Script page is not worth it. A stand-in DOM can always be
    accused of proving nothing - so THE DIFFERENTIAL VALIDATES THE STAND-IN:
    `npm`-free, the test runs the committed handler against BOTH the current file
    and a reconstruction of the old guard, and REQUIRES the old one to fail. If
    the stand-in were not faithful enough to show the defect, that assertion goes
    red and the test tells you so instead of passing quietly.

    The handler source is EXTRACTED from the committed Reception.html, never
    retyped, so the test cannot drift away from the shipped code.

  RUN
    node tests/test_reception_popblock.cjs
*/
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const RECEPTION = path.resolve(__dirname, "..", "apps-script", "governor-page-api", "Reception.html");
const HTML = readFileSync(RECEPTION, "utf8");

/* ---- extract the click handler from the committed file ------------------- */
function extractHandler(src) {
  const start = src.indexOf('document.addEventListener("click", function(e){');
  assert.ok(start > -1, "click handler not found in Reception.html - has it been renamed?");
  const end = src.indexOf("}, true);", start);
  assert.ok(end > -1, "click handler end not found");
  return src.slice(start, end + "}, true);".length);
}

/* ---- the smallest DOM this handler actually touches ---------------------- */
function makeDom() {
  const byId = new Map();
  function mkEl(tag) {
    const el = {
      tagName: tag, id: "", className: "", style: {}, children: [],
      _text: "", parentNode: null,
      get textContent() { return this._text; },
      set textContent(v) {
        this._text = String(v);
        if (this.id) byId.set(this.id, this);
      },
      appendChild(child) {
        if (child.parentNode) {
          const sibs = child.parentNode.children;
          const i = sibs.indexOf(child);
          if (i > -1) sibs.splice(i, 1);   // re-appending MOVES, as in a real DOM
        }
        child.parentNode = this;
        this.children.push(child);
        if (child.id) byId.set(child.id, child);
        return child;
      },
      querySelector(sel) {
        const want = sel.replace(/^\./, "");
        const walk = (n) => {
          for (const c of n.children) {
            if (String(c.className).split(/\s+/).includes(want)) return c;
            const deeper = walk(c);
            if (deeper) return deeper;
          }
          return null;
        };
        return walk(this);
      },
      closest(sel) {
        const want = sel.replace(/^\./, "");
        let n = this;
        while (n) {
          if (String(n.className).split(/\s+/).includes(want)) return n;
          n = n.parentNode;
        }
        return null;
      }
    };
    Object.defineProperty(el, "id", {
      get() { return el._id || ""; },
      set(v) { el._id = v; if (v) byId.set(v, el); }
    });
    return el;
  }
  const body = mkEl("body");
  body.className = "body";
  const document = {
    body,
    createElement: mkEl,
    getElementById: (id) => byId.get(id) || null,
    querySelectorAll: () => [],
    addEventListener(type, fn) { if (type === "click") document._click = fn; }
  };
  return { document, mkEl, byId };
}

function runHandler(handlerSrc, clicks) {
  const { document, mkEl } = makeDom();
  const wrap = mkEl("div");
  wrap.className = "wrap";
  document.body.appendChild(wrap);

  const sandbox = {
    document,
    window: { open() { return null; } },   // "noopener" returns null BY SPEC
    SELF_URL: "",                          // force the window.open path
    VIEWS: {},
    sitePath: () => null,                  // not a site path -> external open
    console
  };
  sandbox.window.document = document;
  vm.createContext(sandbox);
  vm.runInContext(handlerSrc, sandbox);
  assert.ok(typeof document._click === "function", "handler never registered");

  const results = [];
  for (const href of clicks) {
    const a = mkEl("a");
    a.className = "link";
    a.getAttribute = (n) => (n === "href" ? href : null);
    wrap.appendChild(a);
    document._click({
      target: { closest: () => a },
      preventDefault() {}, stopPropagation() {}
    });
    const block = document.getElementById("popblock");
    const addr = block ? block.querySelector(".popaddr") : null;
    results.push({
      shown: addr ? addr.textContent : (block ? block.children[1].textContent : null),
      blocks: countBlocks(document.body)
    });
  }
  return results;
}
function countBlocks(node) {
  let n = String(node.id) === "popblock" ? 1 : 0;
  for (const c of node.children) n += countBlocks(c);
  return n;
}

const VOICE = "https://example.test/voice?session=abc";
const SIGNOUT = "https://example.test/signout";

test("the block shows the address of the link just clicked", () => {
  const r = runHandler(extractHandler(HTML), [VOICE, SIGNOUT]);
  assert.equal(r[0].shown, VOICE, "first click should offer the voice address");
  assert.equal(r[1].shown, SIGNOUT,
    "after clicking Sign out the block must offer the SIGN OUT address, not the stale voice one");
});

test("only ever one block, however many links are clicked", () => {
  const r = runHandler(extractHandler(HTML), [VOICE, SIGNOUT, VOICE]);
  for (const step of r) assert.equal(step.blocks, 1);
});

test("THE DEFECT: the old create-once guard fails this, which proves the stand-in DOM is faithful", () => {
  // Reconstruct the guard exactly as it was at 77ec7e3, wrapped around the same
  // committed body. If the DOM stand-in above were too thin to show the bug,
  // this assertion would go red and the other two would mean nothing.
  const old = extractHandler(HTML)
    .replace(
      /var n = document\.getElementById\("popblock"\), lead, addr;\s*if \(n\) \{[\s\S]*?\} else \{/,
      'if (document.getElementById("popblock")) return;\n      var n, lead, addr;\n      {'
    );
  assert.notEqual(old, extractHandler(HTML), "the old-guard reconstruction did not apply");
  const r = runHandler(old, [VOICE, SIGNOUT]);
  assert.equal(r[0].shown, VOICE);
  assert.equal(r[1].shown, VOICE,
    "the old guard is supposed to leave the STALE voice address here - if this is not stale, " +
    "the stand-in DOM is not reproducing the defect and the other tests prove nothing");
});
