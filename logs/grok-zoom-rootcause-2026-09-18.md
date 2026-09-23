# Zoom root cause (grok dig 2026-09-18)

## Why overlays come back every meeting start
1. `join_and_share.sh` kills Zoom (`pkill -x zoom`) and starts a fresh client each join.
2. Committing share creates NEW `as_toolbar`, `annotate_toolbar`, `zoom_linux_float_video_window`.
3. Overlay park runs once at end of `join_and_share.sh` — not continuous.
4. `as_toolbar` deliberately parked at **0,0** (mute guard) not -3000 — still a black bar for viewers.
5. Alternate `live_share.sh` has **zero** overlay parking.

## Why voice stays robotic/monotone
1. `presenter_say.sh` synthesises with **espeak-ng** on the box by default.
2. Neural `nova` only when laptop ships a WAV via `say_in_zoom.ps1`.
3. No prosody/emotion/style instructions in the TTS path — even nova can sound flat.

## Durable fixes
- Overlay re-park loop while sharing (presenter_loop).
- Ban live_share without park; fix as_toolbar off-capture strategy.
- Require neural TTS + style for live demos; espeak only for offline tests.
