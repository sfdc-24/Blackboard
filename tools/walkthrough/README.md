# Presenter rig for the GCloud migration walkthrough

Operational tooling for a **disposable** GCE instance that joins a Zoom meeting on a
virtual display and shares its screen. It is **not** part of the ORDER product and
nothing in the ORDER runtime imports it. If this directory is unwelcome on `main`,
drop it — it costs nothing to carry on a branch and nothing to remove.

It is committed rather than left in a session scratchpad for one reason: the
walkthrough is a dated commitment, and the scripts had to outlive the session that
wrote them. A runbook that says "copy the script from my scratchpad" is a runbook
that fails the moment the session ends.

## The thing that will catch you

A GCE instance schedule powers the instance **on**. It starts **nothing inside it**.

Xvfb, fluxbox, pulseaudio, pipewire, `xdg-desktop-portal` and the presenter panels are
all started interactively and **none of them survive a stop/start**. The packages and
these scripts *do* survive, because the boot disk is preserved — only the running
processes are gone. A freshly started box answers SSH, looks entirely healthy, and can
do nothing at all.

`presenter_up.sh` is the answer to that. It is idempotent, it holds no credential, and
it prints process counts as returned values rather than claiming success.

## Why portal and pipewire are not optional

Zoom refuses to run its share manager unless `xdg-desktop-portal` and `pipewire` are
present — **even on X11, where it never uses the ScreenCast interface**. Without them
it destroys `SharePresenterModeMgr` and leaves the meeting the instant a share is
committed. That was measured, twice, and it cost hours.

The ScreenCast portal interface itself is genuinely absent on this rig (the GTK backend
only offers it under Wayland). Zoom does not need it. It needs the stack to exist.

## Order of use

| Script | What it does |
|---|---|
| `presenter_up.sh` | Restarts the whole display stack after a stop/start. Start here. |
| `compose2.sh` | Lays out the two panels so a share is not a blank desktop. |
| `presenter_loop.sh` | Top panel: rig state and a board digest **rendered on the laptop**. |
| `presenter_suites_runner.sh` | Runs the ORDER suites, writes a complete result block. |
| `presenter_suites.sh` | Bottom panel: displays that block. Never shows a half-drawn frame. |
| `live_share.sh` | Joins audio, dismisses the banner, opens the share picker. |

Committing the picker is deliberately **not** scripted: the window geometry moves
between frames, so the coordinates are read from the frame you are actually looking at.

## Two rules that are not negotiable

**The join URL never lands here.** It carries a meeting passcode. Write it in a local
file and ship it over stdin — `ssh HOST 'cat > ~/join.url' < local.url` — so it is
never on a command line, never in a process list, and never in this repository. Delete
it at **both** ends afterwards; deleting it from the box alone is not containment while
it still sits on the laptop.

**The bus secret never comes to this box.** The board digest in the top panel is
rendered on the laptop and copied over as plain text. The presenter is a screen, not a
client.

## Proving a share is actually live

A window **title** is not proof. It once read the meeting name while the client sat at
an unclicked Join preview. Proof is the framebuffer: the green border, the banner
`You are screen sharing`, a `Stop share` button, and the sharing-only toolbar. The X11
window names `as_toolbar`, `as_preview`, `cpt_frame_xcb_window` and `annotate_toolbar`
exist only while a share is running and are a good cheap check.

Capture with `import -window root`, pull the PNG, and look at it.

## Never delete the instance

Its boot disk has `autoDelete=true`, so deleting the instance destroys the entire rig —
the display stack, Zoom, the portal fix, PowerShell, the panels and the test copies.
**Stop it. Never delete it.** Creating an instance carries no fee, so deleting to save
money and rebuilding later buys nothing that stopping does not.
