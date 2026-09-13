# Checks that measure the machine, not the landing

Tags: verification, false-green, doctrine

## The pattern

Every instrument built for the presenter rig measures **whether the machine did
the thing**. None of them measures **whether it landed on a person**. Landing is
the only measurement a viewer or a prospect makes.

Four instances in a single day, all found by a human looking, none by a tool:

| What the instrument said | What was true |
|---|---|
| `paplay_rc=0`, `zoom_capture_streams=1` | The client was MUTED. Audio was read from the mic and discarded after the point being measured. |
| "Zoom hides its own share overlays from viewers" | It does not. They arrive as black boxes over the content. |
| Voice synthesised, shipped, played, exit 0 | It sounded "robotic and very unpleasant" — intelligible to a meter, unacceptable to an ear. |
| `fluxbox on :99 already running` | No window manager was running at all. `xprop` exits 0 when it finds nothing. |

## Why the shape recurs

Each check terminates at a boundary the system controls, and the failure lives
one step past that boundary:

- `paplay` returns 0 at the sink. Mute is downstream.
- The share commits successfully. Compositing is downstream.
- The WAV is valid audio. Timbre is not a property the check has.
- `xprop` exits 0. The VALUE, not the exit code, carries the answer.

## What to do about it

1. **Name the boundary.** Write down where the instrument stops and what lies
   past it. `presenter_say.sh` prints `this is not receiver-side proof` for
   exactly this reason — say it in the output, not in a comment.
2. **Get one measurement from the far side.** A second independent client, a
   screenshot pulled and LOOKED at, a human asked one direct question. The
   phone screenshot settled the overlay question in one frame after two days of
   assertion.
3. **Prefer a returned value over a report.** `compose_meeting.sh` passed every
   check — two xterms, two windows, exit 0 — while placing a panel 41px off the
   bottom of the screen. It now reads each window's real geometry off the display
   and refuses. A layout check that never looks at the display is not a layout
   check.

## Related

- [silence-is-not-absence.md](silence-is-not-absence.md)
- [../topics/zoom-headless-presenter.md](../topics/zoom-headless-presenter.md)
