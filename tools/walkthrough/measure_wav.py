#!/usr/bin/env python3
"""Assert that a control recording is silent and a speech recording is not.

WHY THIS IS AN ASSERTION AND NOT A REPORT
    The first version of this file printed UNREADABLE, EMPTY or silence and then
    exited 0 regardless. codex reproduced the consequence in one line: run it against
    a WAV that does not exist and it prints UNREADABLE and returns success. So
    prove_voice.sh - whose entire job was to stop me claiming the meeting heard
    something it did not - could itself pass having measured nothing.

    That is the exact failure this rig keeps producing: a check whose bad outcome is
    a message rather than a non-zero exit. I wrote it into the tool meant to prevent
    it, in a commit whose message was about false greens. So the numbers are now a
    verdict: unreadable, empty, silent-when-it-should-be-loud, loud-when-it-should-be
    -silent, or an insufficient margin between them all exit non-zero.

USAGE
    measure_wav.py --control <silent.wav> --speech <spoken.wav> [--min-margin N]

    Both files must exist, be readable, contain frames, and differ by at least
    --min-margin times in peak amplitude (default 8). A bare list of files may still
    be passed for inspection, but then nothing is asserted and it says so.
"""
import argparse
import array
import math
import os
import sys
import wave

# NO audioop. It was deprecated in 3.11 and REMOVED in 3.13, and this rig runs on
# whatever Python the host happens to have - Ubuntu 26.04 ships 3.13, the GCE box
# ships older. Importing it made this file crash on the newer interpreter, and the
# crash EXITED NON-ZERO, so my own test for "a missing WAV must fail" passed on the
# traceback rather than on the logic it was meant to check. A test that passes for the
# wrong reason is worth less than no test. Peak and RMS are four lines; own them.

_FORMATS = {1: "b", 2: "h", 4: "i"}


def _samples(data, width):
    if width not in _FORMATS:
        raise Unmeasurable("unsupported sample width: %d bytes" % width)
    if width == 1:
        # 8-bit WAV is unsigned, centred on 128.
        return [b - 128 for b in bytearray(data)]
    values = array.array(_FORMATS[width])
    values.frombytes(data[: len(data) - (len(data) % width)])
    if sys.byteorder == "big":
        values.byteswap()
    return values


def _peak(values):
    return max(abs(v) for v in values) if values else 0


def _rms(values):
    if not values:
        return 0
    return int(math.sqrt(sum(float(v) * v for v in values) / len(values)))

SILENCE_CEILING = 0.005   # fraction of full scale a CONTROL may not exceed
SPEECH_FLOOR = 0.05       # fraction of full scale SPEECH must exceed


class Unmeasurable(Exception):
    pass


def measure(path):
    if not os.path.exists(path):
        raise Unmeasurable("%s does not exist - nothing was recorded" % path)
    if os.path.getsize(path) == 0:
        raise Unmeasurable("%s is zero bytes" % path)
    try:
        with wave.open(path, "rb") as handle:
            width = handle.getsampwidth()
            frames = handle.getnframes()
            rate = handle.getframerate()
            data = handle.readframes(frames)
    except Exception as exc:  # noqa: BLE001
        raise Unmeasurable("%s is not a readable WAV: %s" % (path, exc))

    if not data:
        raise Unmeasurable("%s contains no frames" % path)

    values = _samples(data, width)
    peak = _peak(values)
    ceiling = float(2 ** (8 * width - 1))
    return {
        "path": path,
        "seconds": frames / float(rate) if rate else 0.0,
        "peak": peak,
        "rms": _rms(values),
        "fraction": peak / ceiling,
    }


def show(m):
    print("  %-24s %5.1fs  peak=%-8d rms=%-8d  %.3f%% of full scale"
          % (m["path"], m["seconds"], m["peak"], m["rms"], 100.0 * m["fraction"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--control")
    ap.add_argument("--speech")
    ap.add_argument("--min-margin", type=float, default=8.0)
    ap.add_argument("files", nargs="*")
    args = ap.parse_args()

    if not args.control or not args.speech:
        if args.files:
            print("INSPECTION ONLY - nothing is asserted without --control and --speech")
            for path in args.files:
                try:
                    show(measure(path))
                except Unmeasurable as exc:
                    print("  %s" % exc)
            return 0
        ap.error("need --control and --speech")

    try:
        control = measure(args.control)
        speech = measure(args.speech)
    except Unmeasurable as exc:
        print("FAIL unmeasurable: %s" % exc)
        return 1

    show(control)
    show(speech)

    failures = []
    if control["fraction"] > SILENCE_CEILING:
        failures.append("control is NOT silent (%.3f%% > %.3f%%) - something is bleeding "
                        "into the microphone, most likely Zoom's own output routed to the "
                        "mic sink" % (100.0 * control["fraction"], 100.0 * SILENCE_CEILING))
    if speech["fraction"] < SPEECH_FLOOR:
        failures.append("speech is too quiet to be speech (%.3f%% < %.3f%%) - the voice did "
                        "not reach the source Zoom captures"
                        % (100.0 * speech["fraction"], 100.0 * SPEECH_FLOOR))

    # The margin matters independently: two recordings could both sit in a plausible
    # band and still not be distinguishable, which would make the proof meaningless.
    if control["peak"] > 0:
        margin = speech["peak"] / float(control["peak"])
    else:
        margin = float("inf")
    print("  margin speech/control = %s (need >= %.1f)"
          % ("infinite" if margin == float("inf") else "%.1fx" % margin, args.min_margin))
    if margin < args.min_margin:
        failures.append("speech is only %.1fx the control; that is not a distinguishable "
                        "result" % margin)

    if failures:
        for f in failures:
            print("FAIL %s" % f)
        return 1

    print("PASS the control is silent and the speech is loud, by a clear margin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
