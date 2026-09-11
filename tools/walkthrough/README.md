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

> **And on 2026-09-11 the schedule did not fire at all.** The instance read
> `TERMINATED` at 17:01Z, one minute after its start time, and had to be started by
> hand. A peer surface reported "scheduled start succeeded" purely because the box was
> running by then — it was running because a human started it. **Check `status`, never
> the policy.** Verify a scheduled job *ran*; do not verify that it was *scheduled*.

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
| `presenter_voice_up.sh` | Builds the virtual microphone so the box can **speak**. Optional — a silent share still works. |
| `compose2.sh` | Lays out the two panels so a share is not a blank desktop. |
| `presenter_loop.sh` | Top panel: rig state and a board digest **rendered on the laptop**. |
| `presenter_suites_runner.sh` | Runs the ORDER suites, writes a complete result block. |
| `presenter_suites.sh` | Bottom panel: displays that block. Never shows a half-drawn frame. |
| `live_share.sh` | Joins audio, dismisses the banner, opens the share picker. |
| `presenter_say.sh` | Speaks one line into the live meeting. |
| `prove_voice.sh` | Records Zoom's own mic source and measures it against a silent control. |

The board digest goes to **`/tmp/board_digest.txt`**, not `~`. `presenter_loop.sh` reads
only the `/tmp` path; a digest copied to the home directory renders as
"board digest not yet shipped from the laptop" and looks like a broken pipeline.

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

## Giving it a voice

The box has no sound hardware at all, so Zoom finds no input device and opens **no
capture stream**. It will sit in a meeting looking perfectly healthy and transmit
nothing. `presenter_voice_up.sh` builds the path:

```
espeak-ng -> sink 'vmic' -> vmic.monitor -> remap -> source 'vmic_src' -> Zoom
```

The remap is the step that cannot be skipped: **Zoom will not offer a bare `.monitor`
as a microphone.** `module-remap-source` turns it into a device Zoom lists and selects.

Zoom does **not** re-read the default device for a meeting it has already joined, so
the microphone still has to be picked once from the chevron beside the Audio button.

**Point Zoom's speaker at `SFDC24-Speaker`, never at `SFDC24-VirtualMic-Sink.** If its
output lands in the mic sink, Zoom hears itself and everyone else gets an echo. This is
measurable, not theoretical: with the speaker on `vmic` a recording taken with nothing
playing had peak **1239**; with it on `zspk` the same control read peak **0**.

### Proving the voice, which is not the same as playing a file

`paplay` returning 0 means a file played. It says nothing about whether a meeting heard
it — the first test here returned 0 while Zoom had no capture stream and every word went
into the void. `prove_voice.sh` records **from the source Zoom is capturing** and
measures it against a silent control:

| | peak | RMS |
|---|---|---|
| nothing playing | 0 | 0 |
| while speaking | 30,036 | 2,596 |

A capture-stream count of zero means the meeting heard nothing, whatever the exit code.

## Proving a share is actually live

A window **title** is not proof. It once read the meeting name while the client sat at
an unclicked Join preview. Proof is the framebuffer: the green border, the banner
`You are screen sharing`, a `Stop share` button, and the sharing-only toolbar. The X11
window names `as_toolbar`, `as_preview`, `cpt_frame_xcb_window` and `annotate_toolbar`
exist only while a share is running and are a good cheap check.

Capture with `import -window root`, pull the PNG, and look at it.

## Getting in after a stop/start

The instance takes a **new external IP** every time it starts. On Windows, `gcloud
compute ssh` shells out to PuTTY's plink, which blocks on an interactive
"store key in cache?" prompt for the unknown host — so it hangs rather than fails, and
a polling loop will burn its whole timeout looking like the box is not up yet.

Use OpenSSH directly and read the IP from `gcloud` each time:

```
ssh -i ~/.ssh/google_compute_engine -o StrictHostKeyChecking=accept-new user@<ip>
```

The SSH user is `user` — not the Google account name, and not the comment baked into
`google_compute_engine.pub`, which is a local Windows username and will be refused.

## Never delete the instance

Its boot disk has `autoDelete=true`, so deleting the instance destroys the entire rig —
the display stack, Zoom, the portal fix, PowerShell, the panels and the test copies.
**Stop it. Never delete it.** Creating an instance carries no fee, so deleting to save
money and rebuilding later buys nothing that stopping does not.
