# Zoom on a headless presenter box

Tags: zoom, screen-share, xdotool, overlays, mute

## Zoom does NOT hide its own overlays from viewers

**This was asserted twice as "I believe it does" and was wrong.** Zoom's share
furniture — `as_toolbar`, `annotate_toolbar`, `zoom_linux_float_video_window` —
are ordinary X windows sitting on the desktop being captured, so the capture
takes them. They arrive at the viewer as **black boxes** over the content: a bar
across the top, the participant thumbnail on the right, the annotate button
bottom-left.

Settled by a viewer's phone screenshot, not by reasoning.

**Fix:** after the share commits, move them off the captured area.

    for n in as_toolbar annotate_toolbar zoom_linux_float_video_window; do
      for w in $(xdotool search --onlyvisible --name "^${n}$"); do
        xdotool windowmove "$w" -3000 -3000
      done
    done

The share survives — the windows still exist, they are simply not over the
content. Also minimise the `Zoom Workplace` panel, which otherwise covers the
screen with a product pitch and sign-in links.

Source: commit `2d0512a`.

## The meeting window is called `Meeting`, not `Zoom Meeting`

A join detector waiting for `*Zoom*Meeting*` reported NO MEETING WINDOW for 90
seconds **while sitting in the meeting**. A detector that reports failure on
success is worse than none — it invites you to "fix" something that works.

Source: commit `f4be91c`.

## Alt+S is unreliable immediately after joining

On a 15-second join it opened the share picker first time; on a 9-second join it
did nothing. The difference is not the keystroke, it is whether the client has
finished setting itself up. Settle ~8s, then retry up to three times, checking
for the picker between attempts rather than assuming the press landed.

Source: commit `f4be91c`.

## The share picker's Share button is 53px above its reported bottom

`xdotool getwindowgeometry` disagrees with what is drawn by ~26px: it reports
`Y=231 HEIGHT=640` implying a bottom at 871, while the picker visibly ends at 845
and the button centre is at 818. `Return` does NOT commit the picker.

Assert on `as_toolbar` existing, not on the click having happened.

Source: commit `f4be91c`.

## Zoom can join MUTED, and every audio check still passes

The single most expensive failure of the day. Audio was synthesised, shipped
byte-identical, played into the virtual microphone, and Zoom confirmed reading
that microphone:

    spoke_bytes=657644 sink=vmic source=vmic_src paplay_rc=0 zoom_capture_streams=1

All true. All useless — **Zoom's mute switch sits after everything being
measured.** It was reading the mic and discarding the audio.

Found by looking at the participant thumbnail in a screenshot and seeing a red
line through the mic icon. Not by any instrument.

### Nothing in PulseAudio can see Zoom's mute

Measured, not assumed. With the client definitively muted — red slash on the
Audio button, the participant thumbnail and the sharing bar — PulseAudio still
reported the Zoom source-output as `Corked: no` and `Mute: no`, and
`zoom_bound_count` still returned 1. Toggled twice to be certain.

**Zoom's mute is internal to Zoom.** Any check built on `pactl` will report a
healthy audio path into a muted client, forever.

### So read the toolbar

Zoom draws a crimson slash across the Audio button when muted. Counting strongly
red pixels in that button's region separates the states with an enormous margin,
calibrated over two full toggle cycles on a live client:

    muted   : 108 red pixels
    unmuted :   0

`presenter_mute_state.sh` does this and returns **0 open / 1 muted / 2 unknown**.
Exit 2 is the point: the toolbar only exists while sharing and cannot be read
when parked off-screen, and "I could not look" must never collapse into "I looked
and it is fine". `presenter_say.sh` now refuses to play when it sees 1, and warns
loudly on 2 rather than proceeding silently.

`Alt+A` toggles mute — but it is as unreliable as `Alt+S` immediately after
joining; confirm the state flipped rather than assuming the keystroke landed.

See also: [../concepts/checks-that-measure-the-machine.md](../concepts/checks-that-measure-the-machine.md)

## Joining a room that has only just opened takes ~10x longer

Rehearsals measured 9–15s, every one of them against a warm client joining an
already-open room. The real first join into a room at its opening minute took
**108 seconds**. Start early enough that this is invisible.

## Zoom needs xdg-desktop-portal and pipewire even on X11

Without them it destroys `SharePresenterModeMgr` and leaves the meeting the
instant a share is committed.
