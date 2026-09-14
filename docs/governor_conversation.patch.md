# Governor console: a conversation instead of a status card

`gas/Index.html` is untracked — it is live Apps Script source and carries live
identifiers (see `.gitignore`). So the change lives here, in the same form as
`monitor_silence_guard.patch.md` and `tts_cap_guard.patch.md`.

- **Author:** claude-code-cli, 2026-09-08
- **Status:** applied locally, **NOT DEPLOYED**. He asked to see it first and it
  is his interface.
- **Preview for him:** https://claude.ai/code/artifact/900985f1-f1f4-4a0f-b7ad-8d043436ed87
- **Tests:** `tests/test_thread_view.js` — 24 assertions, all passing locally.
  It lifts `threadRows` and `who` out of the page rather than copying them, so it
  fails if the page changes and the test does not.

  **It is deliberately not in this PR.** It reads `gas/Index.html`, which is
  gitignored, so on a clean checkout it cannot run at all — it would sit in CI
  looking green while proving nothing, which is the failure this repo keeps
  catching in other people's work. The test travels with the change: when the
  conversation view is applied to `apps-script/governor-page-api/Index.html`, the
  test comes with it and reads that file instead.
- **Deploy — READ THIS FIRST.**

  *The hazard, 2026-09-08:* this file originally said to ship with `clasp push`
  from `gas/`. `gas/` was **diverged, not merely behind** — `Code.js` 760 lines
  against canonical's 1034 and missing the server-signed conversation identity,
  `Auth.gs` 310 against 409, `Reception.html` on a v31 base — while `Index.html`
  matched canonical exactly. `clasp` pushes the whole `rootDir`, so that
  instruction would have put a stale backend over the merged tenant-boundary
  work. The patch was safe; the push route was not.

  *Reconciled the same day.* `gas/` is now rebuilt from
  `apps-script/governor-page-api/` on `origin/main`, plus exactly two things in
  review: this `Index.html` change, and the `Reception.html` voice link from
  [PR #33](https://github.com/sfdc-24/Blackboard/pull/33). Verified after the
  rebuild — `mintConversation_`/`conversationIdentity_` present, the thread view
  intact, the voice link intact, every file parses, tests green. So a push can
  no longer *revert* anything.

  *It still is not mine to push.* Shipping canonical's backend **is** the coupled
  release in `docs/MULTITENANT-READINESS.md`, and that sequence is codex's. The
  route for this change stays: PR it against
  `apps-script/governor-page-api/Index.html` and let that release carry it.

  Also do **not** copy `site/` — see SITE-MIRROR-DRIFT-001.

## Why

His ask, verbatim:

> you should be able to interface with me via sfdc24.com governor page just the
> way we are interfacing here

Half of it already existed, and I nearly missed that. `postRow` is
governor-guarded, the page already had a message box, and his notes were already
reaching the board. What the page could not do was show him an **exchange**. It
showed **state** — now / next / blocked — refreshed every sixty seconds whether
or not anyone had said anything. He could watch a card change. He could not read
a reply.

That is why *"Show me what you can do"*, sent from that box at
`2026-09-07T03:39:45Z`, sat for twenty-two hours.

## The three decisions worth defending

**Who spoke is decided by the server-stamped source column, never by `tag=`.**
`postRow` stamps `governor-page`; an instance writing through the bus carries its
own tag. `tag=` is free text inside the payload, and three `vm-chrome` rows on
the live board today carry `tag=GOVERNOR` while *quoting* a decision rather than
making one. Keyed the easy way, those render on his own console as him.

**A sent message stays dim until a read-back carries it.** D-4: read-back is the
only proof of a write, so an unconfirmed bubble must not look identical to a
delivered one. A failed write turns red and stays on screen — it is never quietly
removed, because a message that vanishes reads as a message that arrived.

**Polling stops entirely while the tab is hidden**, runs at 10s while he is in
the conversation and 45s while he is not. `setInterval(load,60000)` ran all night
in a background tab — roughly 480 reads a day for nobody. Faster when he is here,
cheaper when he is not, which matters because Apps Script execution time is a
daily budget rather than a free one.

## What is deliberately unchanged

- The `activity` aside still shows the same feed newest-first. Two views of one
  set of rows: the thread is for reading and replying, the log is for scanning
  what happened while he was away. If he finds it redundant it is one line to
  remove — his call, not mine.
- `postRow`, the governor guard, the passphrase fallback and the payload grammar
  are untouched. This changes how the rows are displayed and how often they are
  fetched. It adds no new write path and no new capability.

## The patch

```diff
--- gas/Index.html (before)
+++ gas/Index.html (after)
@@ CSS, after .rule .n
+/* conversation.
+   He asked to talk to the fleet here the way he talks to it in a terminal. The
+   page could already SEND -- what it could not do was show him an exchange. It
+   showed STATE: a card that refreshed every sixty seconds whether or not anyone
+   had said anything. So the same feed rows are laid out as a thread instead.
+   His messages sit right and green; everything else sits left with the name of
+   whoever wrote it, because "who said this" was the first thing he asked for
+   (2026-09-07T20:34Z, on WhatsApp) and the first thing this view owes him. */
+.thread{max-height:52vh;overflow-y:auto;overscroll-behavior:contain;display:flex;flex-direction:column;gap:10px;padding:2px 2px 10px;margin-bottom:12px;border-bottom:1px solid var(--line)}
+.msg{max-width:78%;align-self:flex-start;padding:8px 12px;border:1px solid var(--line);border-radius:11px;background:var(--bg2);color:var(--text);font-size:13px;line-height:1.55;overflow-wrap:anywhere}
+.msg.me{align-self:flex-end;background:var(--acc);border-color:var(--acc);color:#04120C}
+.msg .from{display:block;font:600 11px/1.2 var(--mono);opacity:.72;margin-bottom:4px}
+.msg .when{display:block;font-size:10.5px;opacity:.62;margin-top:5px}
+/* Dimmed until the board has been read back. D-4: a POST that returned 200 is
+   not proof the row landed, so an unconfirmed message must not look identical
+   to a confirmed one. */
+.msg.pending{opacity:.5}
+.msg.failed{background:var(--bg2);border-color:var(--red);color:var(--red)}
+.threadempty{color:var(--mut);font-size:12.5px;padding:10px 2px}
+@media(max-width:640px){.thread{max-height:46vh}.msg{max-width:88%}}

@@ markup, in <main>
-      <h2>message</h2>
+      <h2>conversation</h2>
       <div class="dcard">
+        <div class="thread" id="thread"><div class="threadempty">loading…</div></div>
         <textarea id="noteText" rows="2" placeholder="a direction, a note, or a new ask."></textarea>

@@ build(), the feed case
-        case "feed": st.feed.push({tag:g.tag||r.source||"",text:g.text||"",at:at});break;
+        // r.source is stamped by the server: postRow writes `governor-page` and a
+        // fleet instance writing through the bus carries its own tag. Keep it --
+        // the thread decides who is speaking from THIS, never from g.tag, which is
+        // free text any writer can set to GOVERNOR and which three vm-chrome rows
+        // on this board already do.
+        case "feed": st.feed.push({id:r.id||"",src:r.source||"",tag:g.tag||r.source||"",text:g.text||"",at:at});break;

@@ new state, beside `var data=null, user=null, current=null, expanded={};`
+  // "Is he in the middle of talking to us right now" -- drives the poll rate.
+  var lastTouch=0;
+  function touch(){lastTouch=Date.now()}

@@ new functions, before renderDecisions()
+  var pend=[];
+  function threadRows(st){
+    var rows=(st.feed||[]).slice().sort(function(a,b){return new Date(a.at)-new Date(b.at)}).slice(-40);
+    var confirmed={};
+    rows.forEach(function(f){ if(f.src==="governor-page") confirmed[f.text]=true });
+    pend=pend.filter(function(x){ return !(confirmed[x.text] && !x.failed) });
+    return rows.concat(pend);
+  }
+  function who(f){
+    if(f.src==="governor-page")return{me:true,name:"you"};
+    var n=(f.tag||f.src||"instance").toLowerCase();
+    return{me:false,name:n};
+  }
+  function renderThread(st){
+    var el=$("thread"); if(!el)return;
+    var atEnd=(el.scrollHeight-el.scrollTop-el.clientHeight)<48;
+    var rows=threadRows(st);
+    if(!rows.length){el.innerHTML='<div class="threadempty">nothing said here yet. type below and an instance picks it up.</div>';return}
+    el.innerHTML=rows.map(function(f){
+      var w=who(f), cls="msg"+(w.me?" me":"")+(f.pending?" pending":"")+(f.failed?" failed":"");
+      var when=f.failed?"could not send — nothing was written":(f.pending?"sending…":esc(fmt(f.at)));
+      return '<div class="'+cls+'"><span class="from">'+esc(w.name)+'</span>'+md(f.text)+'<span class="when">'+when+'</span></div>';
+    }).join("");
+    if(atEnd)el.scrollTop=el.scrollHeight;
+  }

@@ renderProject()
     renderDecisions();
+    renderThread(st);
     $("feed").innerHTML=...

@@ load() must return its promise so the poll can chain off it
-    call("getState",pass).then(function(j){
+    return call("getState",pass).then(function(j){

@@ send: optimistic echo, then confirm by read-back
-  $("noteSend").addEventListener("click",function(){ ...one-liner... });
+  function send(){
+    var v=$("noteText").value.trim(); if(!v){toast("nothing to send");return}
+    var clean=v.replace(/\|/g,"/").slice(0,2000);
+    var echo={id:"",src:"governor-page",tag:"GOVERNOR",text:clean,at:new Date().toISOString(),pending:true};
+    pend.push(echo);
+    $("noteText").value="";
+    if(current&&data&&data.states[current])renderThread(data.states[current]);
+    touch();
+    post("GOV|kind=feed|tag=GOVERNOR|text="+clean).then(function(){
+      return load();          // still pending: only a read-back proves the row
+    }).catch(function(err){
+      if(err.message==="no auth"){
+        pend=pend.filter(function(x){return x!==echo});   // nothing was written
+        $("noteText").value=v;
+      } else {
+        echo.pending=false; echo.failed=true;
+        toast("send failed: "+err.message);
+      }
+      if(current&&data&&data.states[current])renderThread(data.states[current]);
+    });
+  }
+  $("noteSend").addEventListener("click",send);
+  $("noteText").addEventListener("keydown",function(e){
+    if(e.key==="Enter"&&!e.shiftKey&&!e.ctrlKey&&!e.metaKey){e.preventDefault();send()}
+  });
+  $("noteText").addEventListener("input",touch);

@@ polling
-  load();
-  setInterval(load,60000);
+  var timer=null;
+  function nextDelay(){
+    if(document.hidden)return 0;
+    return (Date.now()-lastTouch<180000)?10000:45000;
+  }
+  function schedule(){clearTimeout(timer);var d=nextDelay();if(d)timer=setTimeout(tick,d)}
+  function tick(){load().then(schedule,schedule)}
+  document.addEventListener("visibilitychange",function(){
+    if(document.hidden){clearTimeout(timer);return}
+    touch();tick();
+  });
+  load().then(schedule,schedule);
```

## Known edges, written down rather than discovered later

- If he sends the **same text twice in a row**, the second optimistic echo is
  dropped as soon as the board carries the first. The board still carries both,
  so the next poll shows both; the visible effect is one message flickering out
  for a few seconds. Left alone: the alternative is matching on a server-minted
  row id the client does not have at send time.
- The thread shows the **last 40** feed rows for the selected project. Older
  history is on the board and is not paged in. If he asks to scroll back, that
  is a `getState` change, not a view change.
- **`Enter` sends.** He works in a terminal, so this is the expected key. If he
  wants a multi-line note it is `shift+enter`, and the hint says so.
