#!/usr/bin/env python3
"""A deterministic page agent. No model, no tokens, no guessing.

WHY THIS EXISTS
  Mr Salam, 2026-09-17: "you should definitely think of making an agent out of
  python that does basic stuff like map out the website, anticipate actions,
  take notes etc so we are not burning budget and tokens on you or other AI
  Agents."

  He is right, and the evidence is the whole evening. Every failed click was the
  same mistake: read a coordinate off a screenshot, then click that pixel later,
  after the window had resized or the page had scrolled. 572,837 was correct
  once and wrong three times. A screenshot is a photograph of the past; the DOM
  is the present.

  This talks to Chrome's DevTools protocol over the debug port and asks the page
  where things ARE. It cannot be wrong about a button's position, because it
  does not remember one - it looks.

WHAT IT DOES
  map      - every clickable thing, with live coordinates and visibility
  click    - click by TEXT, resolved at the moment of clicking
  notes    - a structural snapshot: headings, landmarks, console lines, state
  watch    - poll for what changed after an action (anticipate/verify)

USAGE
  python3 page_agent.py map
  python3 page_agent.py click "Approvals that sit for days"
  python3 page_agent.py notes
  python3 page_agent.py watch 6

DESIGN RULES, each one a bug from tonight
  1. Never return a coordinate for something invisible. checkVisibility() is the
     authority - a page once passed a text check while the text was hidden.
  2. Never report success from a command's exit code. Read the result back and
     compare. "clicked" printed happily three times while nothing was hit.
  3. Say NOT FOUND loudly. A silent no-op is worse than an error, because it
     looks like it worked.
"""
import json
import sys
import time
import urllib.request

DEBUG = "http://127.0.0.1:9222"


def _http(path):
    with urllib.request.urlopen(DEBUG + path, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def page_target():
    """The first real page target, skipping devtools and extension targets."""
    for t in _http("/json"):
        if t.get("type") == "page" and not t.get("url", "").startswith("devtools://"):
            return t
    raise SystemExit("no page target on the debug port - is Chrome running with --remote-debugging-port?")


def evaluate(expr):
    """Run JS in the page and return its value.

    USES A REAL LIBRARY, DELIBERATELY. The first version hand-rolled the
    WebSocket framing — masking, length headers, the lot — and it failed with
    ConnectionResetError on the first read. The handshake was fine (Chrome
    answered `101 WebSocket Protocol Handshake` with a valid
    Sec-WebSocket-Accept), so the fault was somewhere in my own frames or the
    Origin header I was sending.

    Rather than debug protocol code by guesswork, `python3-websocket` 1.7.0 is
    installed from apt. Hand-rolled protocol implementations are the exact
    category of code that looks correct and is not, which is the lesson of this
    whole evening — a library that thousands of people exercise daily is worth
    more than a clever fix of mine.
    """
    import websocket  # python3-websocket, apt

    ws_url = page_target()["webSocketDebuggerUrl"]
    # suppress_origin: Chrome 153 resets DevTools sockets that carry an
    # unexpected Origin unless the browser was launched with a matching
    # --remote-allow-origins. Sending none sidesteps it entirely.
    conn = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
    try:
        conn.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
        }))
        # The socket carries unsolicited events too, so match on the id rather
        # than trusting that the next frame is the answer.
        for _ in range(40):
            data = json.loads(conn.recv())
            if data.get("id") != 1:
                continue
            res = data.get("result", {}).get("result", {})
            if data.get("result", {}).get("exceptionDetails"):
                raise SystemExit(f"JS threw: {data['result']['exceptionDetails'].get('text')}")
            return res.get("value", res)
        raise SystemExit("no reply to the evaluate request after 40 frames")
    finally:
        conn.close()


MAP_JS = r"""
(() => {
  const sel = 'a[href], button, input, textarea, [role="button"], [onclick]';
  const out = [];
  document.querySelectorAll(sel).forEach((el, i) => {
    const r = el.getBoundingClientRect();
    // checkVisibility is the authority. A page here once passed a text guard
    // while the text was inside a hidden subtree.
    const vis = el.checkVisibility ? el.checkVisibility() : (r.width > 0 && r.height > 0);
    if (!vis || r.width === 0 || r.height === 0) return;
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
    out.push({
      i, tag: el.tagName.toLowerCase(), label: label.slice(0, 80),
      x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width), h: Math.round(r.height),
      inView: r.top >= 0 && r.bottom <= innerHeight,
      href: el.getAttribute('href') || null
    });
  });
  return { url: location.href, scrollY: Math.round(scrollY),
           docH: Math.round(document.body.scrollHeight), winH: innerHeight, items: out };
})()
"""

NOTES_JS = r"""
(() => {
  const txt = (el) => (el ? el.innerText.trim().replace(/\s+/g, ' ') : null);
  const heads = [...document.querySelectorAll('h1,h2,h3')]
    .filter(h => !h.closest('[hidden]'))
    .map(h => ({ level: h.tagName, text: txt(h).slice(0, 90) }));
  const cli = [...document.querySelectorAll('#cliout > div')].map(d => txt(d));
  const lanes = [...document.querySelectorAll('.lane')].map(l => ({
    who: txt(l.querySelector('.who')),
    doing: txt(l.querySelector('.doing')),
    badge: txt(l.querySelector('.badge')),
    working: l.classList.contains('on')
  }));
  const chips = [...document.querySelectorAll('.chip')].map(c => txt(c));
  return {
    title: document.title, url: location.href,
    headings: heads, chips, lanes, console: cli,
    scrollable: document.body.scrollHeight > innerHeight,
    overflowPx: Math.max(0, document.body.scrollHeight - innerHeight)
  };
})()
"""


def cmd_map():
    d = evaluate(MAP_JS)
    print(f"  url        {d['url']}")
    print(f"  scroll     {d['scrollY']} of {d['docH'] - d['winH']} ({d['docH']}px doc, {d['winH']}px window)")
    print(f"  clickable  {len(d['items'])} visible elements\n")
    for it in d["items"]:
        mark = " " if it["inView"] else "*"
        label = it["label"] or "(no text)"
        print(f"  {mark}{it['tag']:<8} {it['x']:>5},{it['y']:<5} {label[:56]}")
    print("\n  * = present but scrolled out of view")


def cmd_click(text):
    """Click by text, resolved NOW. Reads back to prove something happened."""
    js = """
    (() => {
      const want = %s.toLowerCase();
      const els = [...document.querySelectorAll('a[href], button, [role="button"], .chip')];
      const el = els.find(e => (e.innerText || '').trim().toLowerCase().includes(want));
      if (!el) return { ok: false, reason: 'NOT FOUND' };
      const vis = el.checkVisibility ? el.checkVisibility() : true;
      if (!vis) return { ok: false, reason: 'found but not visible' };
      el.scrollIntoView({ block: 'center' });
      const r = el.getBoundingClientRect();
      el.click();
      return { ok: true, label: (el.innerText || '').trim().slice(0, 60),
               x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
    })()
    """ % json.dumps(text)
    before = evaluate("document.querySelectorAll('#cliout > div').length")
    res = evaluate(js)
    if not res.get("ok"):
        print(f"  CLICK FAILED: {res.get('reason')} for {text!r}")
        return 1
    print(f"  clicked {res['label']!r} at {res['x']},{res['y']}")
    time.sleep(1.5)
    after = evaluate("document.querySelectorAll('#cliout > div').length")
    print(f"  console lines {before} -> {after}" + ("  (page reacted)" if after > before else "  (no console change)"))
    return 0


def cmd_notes():
    d = evaluate(NOTES_JS)
    print(f"  title      {d['title']}")
    print(f"  url        {d['url']}")
    print(f"  scrollable {d['scrollable']}  (overflow {d['overflowPx']}px)")
    print(f"\n  headings ({len(d['headings'])}):")
    for h in d["headings"]:
        print(f"    {h['level']:<3} {h['text']}")
    print(f"\n  chips ({len(d['chips'])}): " + ", ".join(d["chips"]))
    print(f"\n  lanes ({len(d['lanes'])}):")
    for l in d["lanes"]:
        state = "WORKING" if l["working"] else "resting"
        print(f"    {state:<8} {l['who']:<22} {l['badge']:<9} {l['doing']}")
    print(f"\n  console ({len(d['console'])}):")
    for c in d["console"]:
        print(f"    {c}")


def cmd_watch(seconds):
    """Anticipate: snapshot, wait, report what actually changed."""
    a = evaluate(NOTES_JS)
    time.sleep(seconds)
    b = evaluate(NOTES_JS)
    changed = []
    if len(a["console"]) != len(b["console"]):
        changed.append(f"console {len(a['console'])} -> {len(b['console'])} lines")
        for line in b["console"][len(a["console"]):]:
            changed.append(f"  new: {line}")
    for x, y in zip(a["lanes"], b["lanes"]):
        if x["working"] != y["working"]:
            changed.append(f"lane {y['who']}: {'woke' if y['working'] else 'slept'}")
        if x["doing"] != y["doing"]:
            changed.append(f"lane {y['who']} caption: {y['doing']}")
    print("  no change" if not changed else "\n".join("  " + c for c in changed))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    cmd = sys.argv[1]
    if cmd == "map":
        cmd_map()
    elif cmd == "click":
        raise SystemExit(cmd_click(" ".join(sys.argv[2:])))
    elif cmd == "notes":
        cmd_notes()
    elif cmd == "watch":
        cmd_watch(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    else:
        raise SystemExit(f"unknown command {cmd!r}")
