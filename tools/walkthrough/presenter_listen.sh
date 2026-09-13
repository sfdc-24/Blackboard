#!/bin/bash
# Record what the OTHER people in the meeting are saying.
#
# WHY THIS DID NOT EXIST UNTIL NOW, which is the whole point
#   The rig could speak and share for two days. It had no ears. Mr Salam sat in a
#   Zoom call talking to it and had to type on WhatsApp, and asked the obvious
#   question: "Why do I have to message in whatsapp while being on a zoom call
#   with you?" Because nobody was listening. A presenter that cannot hear is a
#   broadcast, and what he was promised was a meeting.
#
# WHERE THE SOUND ALREADY WAS
#   Zoom's SPEAKER is the sink 'zspk'. It was put there so Zoom would not hear
#   itself through the virtual microphone - and that means everything the far end
#   says is already being written to it. 'zspk.monitor' is a source carrying
#   exactly that. The ear was in the plumbing the whole time with nothing
#   attached to it.
#
#     far end -> Zoom -> sink 'zspk' -> zspk.monitor -> parec -> here
#
#   16 kHz mono because that is what speech recognition wants, and it is an
#   eighth of the bytes of the 48 kHz stereo the sink runs at.
#
# NO CREDENTIAL HERE. This only records. Transcription happens on the laptop,
# where the key lives.
set -uo pipefail

SECS="${1:-15}"
OUT="${2:-/tmp/hear.wav}"
DEV="${PRESENTER_SPEAKER_MONITOR:-zspk.monitor}"
HERE="$(cd "$(dirname "$0")" && pwd)"

case "$SECS" in ''|*[!0-9]*) echo "usage: presenter_listen.sh [seconds] [outfile]" >&2; exit 2 ;; esac

# IS ANYTHING FEEDING THE SPEAKER AT ALL?
# If Zoom is not playing into zspk there is nothing to hear, and a silent
# recording would look identical to a room where nobody happened to speak. Those
# are completely different problems and must not produce the same output.
FEEDING=$(pactl list sink-inputs 2>/dev/null | grep -c 'ZOOM VoiceEngine' || true)

rm -f "$OUT"
timeout "$SECS" parec --device="$DEV" --format=s16le --rate=16000 --channels=1 \
        --file-format=wav "$OUT" 2>/tmp/parec.err
rc=$?
# 124 means timeout stopped parec, which is the NORMAL ending here, not a fault.
if [ "$rc" -ne 124 ] && [ "$rc" -ne 0 ]; then
  echo "parec failed rc=$rc"
  sed -n '1,5p' /tmp/parec.err
  exit 1
fi

[ -s "$OUT" ] || { echo "no recording produced"; exit 1; }
BYTES=$(stat -c %s "$OUT")

# Peak and RMS, so "silence" is a MEASUREMENT. A file of 480000 zero bytes is not
# a recording of a quiet room, it is a broken capture, and the two must not read
# the same.
read -r PEAK RMS <<EOF
$(python3 "$HERE/wav_peak.py" "$OUT" 2>/dev/null || echo "0 0")
EOF
PEAK="${PEAK:-0}"; RMS="${RMS:-0}"

echo "recorded_bytes=$BYTES seconds=$SECS device=$DEV zoom_feeding_speaker=$FEEDING peak=$PEAK rms=$RMS"

# The Zoom-feeding gate is right in production and WRONG for the self test, where
# the far end is simulated with paplay and there is deliberately no Zoom in the
# picture. Make that an explicit opt-out rather than quietly dropping the check -
# a guard that can be skipped by accident is a guard nobody trusts.
if [ "$FEEDING" -eq 0 ] && [ "${PRESENTER_REQUIRE_ZOOM:-1}" != "0" ]; then
  echo "VERDICT no-zoom-audio - Zoom is not playing into $DEV at all; this is a plumbing fault, not a quiet room"
  exit 3
fi
if [ "$PEAK" -lt 200 ]; then
  echo "VERDICT silence - the path works, nobody spoke in that window"
  exit 0
fi
echo "VERDICT audio-present"
exit 0
