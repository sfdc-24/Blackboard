# The presenter audio path

Tags: audio, pulseaudio, pipewire, tts, whisper, cpu

## The chain

    speaking:  WAV -> sink 'vmic' -> vmic.monitor -> remap -> source 'vmic_src' -> Zoom
    hearing:   far end -> Zoom -> sink 'zspk' -> zspk.monitor -> parec -> Whisper

Zoom's SPEAKER must be on the separate `zspk` sink. If it points at `vmic`, Zoom
hears itself: a "silent" control recording came back loud.

That separation is also what makes hearing possible — everything the far end says
is already being written to `zspk`, and `zspk.monitor` is a source on it. The ear
was in the plumbing for two days with nothing attached.

## The voice broke up because the box was too small

Not the WAV, not the resampling, not `paplay`. The box was an `e2-medium` — 2
vCPU — with load average **2.28**:

    zoom     54.8% CPU
    Xvfb     17.8%
    cpthost  15.5%   (Zoom's screen capture)

Audio breaks when the scheduler cannot service the audio thread in time.
Resized to `e2-standard-4`: load 1.56 on four cores, and a slow count of ten
recorded off `vmic_src` — what Zoom actually reads — came back complete and in
order.

**Check CPU headroom before debugging an audio pipeline.**

## espeak-ng is intelligible and unacceptable

It is a formant synthesiser: no network, no key, and it sounds like a machine.
Correct for proving the box can make a sound; wrong in front of a client, whose
verdict was "robotic and very unpleasant".

Replaced with OpenAI `nova` via `gpt-4o-mini-tts`, synthesised **on the laptop**
and shipped to the box as a WAV. The key stays on the laptop: the box is
disposable, gets destroyed and rebuilt, and its Zoom client writes its own launch
URL into a log. **A WAV is not a credential; an API key is.**

`presenter_say.sh` gained `PRESENTER_WAV` to play a supplied recording instead of
synthesising. Every existing guard survived.

## Refuse a file that does not start with RIFF

A half-shipped or truncated download makes `paplay` play nothing and return 0 —
a silent success. Check the header AND compare byte counts end to end, because a
truncated WAV still has a valid RIFF header.

## Silence must be a measurement

A recording of 480000 zero bytes is not a recording of a quiet room, it is a
broken capture, and the two must not read the same. `presenter_listen.sh` reports
peak and RMS, and separately refuses when Zoom is not feeding the speaker sink at
all — a plumbing fault and a quiet room need different actions.

## Hearing proves itself without a human

`-SelfTest` speaks a known sentence into the SPEAKER sink (pretending to be the
far end), records the monitor, transcribes, and requires the words back.

    peak=20359 rms=1768  VERDICT audio-present
    HEARD: The quick brown fox jumps over the lazy dog.
    SELF TEST: 6 of 6 key words came back

A hearing test that needs a volunteer to talk is a test that never gets run.

## Groq model notes

`llama-3.3-70b-versatile` was retired — 404. `qwen/qwen3.6-27b` works but is a
reasoning model: left alone it spends its output budget thinking and the JSON is
truncated, which Groq reports as `400 Failed to validate JSON` — reading like a
bad prompt when it is a truncated one. `reasoning_effort='none'` is load-bearing.
Free tier caps **output tokens per minute at 1000, measured on requested
max_tokens**, so a 429 there is a cap and not congestion.
