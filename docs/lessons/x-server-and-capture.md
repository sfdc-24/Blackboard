# Screenshots, X grabs, and wedging a display

Tags: xorg, import, imagemagick, xdotool, screenshots

## Use `import -window root`. Never `import -window <id>`.

Every successful capture on the presenter rig has used `import -window root`
followed by a `-crop`. Every hang has come from trying to grab a specific window.

On 2026-09-12 one mis-quoted call took the display down for **fourteen minutes**:

    import -window '"$tb"' /tmp/w.png     # the literal string $tb, not the id

`import` could not resolve that as a window, so it fell back to **interactive
mode** — waiting for a human to click a window to capture. There is no human on a
headless box. It waited, and while waiting it held an **X server grab**.

An X server grab blocks every other X client. `xdotool` stopped answering. Further
captures blocked. The whole display was unusable while Zoom was mid-share.

## Killing the `import` does not fix it

`pkill -x import` appeared to work — the count went to zero — and the display was
still wedged seconds later. The reason: the **parent** shell was still looping and
spawned a new `import` each time one died. It had been running 827 seconds.

    ps -eo pid,stat,etimes,comm,args | grep -i import

showed the orphaned parent immediately. **Kill the parent, not the symptom.** Two
rounds of whack-a-mole were spent on the child.

## Rules that follow

- `import -window root` only, then crop from a window's geometry via
  `xdotool getwindowgeometry --shell`.
- Wrap **every** `import` in `timeout`. A capture that hangs takes the display
  with it, which during a live meeting is worse than any wrong answer.
- Sweep stray `import` processes between automated captures.
- When a display stops responding, look for a held grab before suspecting X,
  the driver, or the window manager. It is almost always a capture tool waiting
  for input that will never arrive.

## A window parked off-screen cannot be root-captured

Obvious in hindsight, and it constrains design. `join_and_share.sh` parks Zoom's
overlays at `-3000,-3000` so they stay out of the shared frame — which also puts
them beyond the reach of a root capture. Anything that must be *read* from those
windows has to stay on-screen.

Hence `presenter_mute_state.sh` returns **exit 2 (UNKNOWN)** when the toolbar is
off-screen, rather than a comfortable "not muted".

## A window at the bottom of the screen may be unclickable

Parked along the bottom edge, Zoom's share toolbar sat behind the Chrome kiosk
window. Nine synthetic clicks — including after `xdotool windowraise` — never
reached Zoom. Position affects *interaction*, not just visibility, and a click
that lands on the wrong window looks exactly like a broken detector.
