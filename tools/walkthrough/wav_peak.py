#!/usr/bin/env python3
"""Print "peak rms" for a 16-bit PCM WAV, or "0 0" if it cannot be measured.

Separate file rather than inline in the shell script on purpose: the first
version embedded this in a heredoc inside another heredoc, which is exactly the
shape that has corrupted scripts in this repo before.

No audioop - it was removed in Python 3.13 and a previous proof in this rig
"passed" on the traceback that caused.
"""
import struct
import sys
import wave


def main() -> int:
    if len(sys.argv) < 2:
        print("0 0")
        return 2
    try:
        with wave.open(sys.argv[1], "rb") as w:
            if w.getsampwidth() != 2:
                print("0 0")
                return 1
            data = w.readframes(w.getnframes())
    except Exception:
        print("0 0")
        return 1

    if not data:
        print("0 0")
        return 1

    count = len(data) // 2
    samples = struct.unpack("<%dh" % count, data[: count * 2])
    peak = 0
    total = 0
    for v in samples:
        a = -v if v < 0 else v
        if a > peak:
            peak = a
        total += v * v
    rms = int((total / count) ** 0.5) if count else 0
    print(peak, rms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
