#!/usr/bin/env python3
"""Report peak and RMS amplitude for each WAV given.

The point is to tell SOUND from SILENCE with a number rather than a claim. A recording
taken off Zoom's own microphone source is the only thing that shows the synthesised
speech actually arrives where Zoom reads it; a file existing proves nothing, and an
exit code of zero proves less.
"""
import sys
import wave
import audioop


def describe(path):
    try:
        with wave.open(path, "rb") as handle:
            width = handle.getsampwidth()
            frames = handle.getnframes()
            rate = handle.getframerate()
            data = handle.readframes(frames)
    except Exception as exc:  # noqa: BLE001
        print("  %-22s UNREADABLE: %s" % (path, exc))
        return

    if not data:
        print("  %-22s EMPTY" % path)
        return

    peak = audioop.max(data, width)
    rms = audioop.rms(data, width)
    ceiling = float(2 ** (8 * width - 1))
    seconds = frames / float(rate) if rate else 0.0
    verdict = "SOUND" if peak > ceiling * 0.01 else "silence"
    print("  %-22s %5.1fs  peak=%-8d rms=%-8d  %.2f%% of full scale  -> %s"
          % (path, seconds, peak, rms, 100.0 * peak / ceiling, verdict))


for arg in sys.argv[1:]:
    describe(arg)
