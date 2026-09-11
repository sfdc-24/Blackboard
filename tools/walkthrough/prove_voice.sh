#!/bin/bash
# Prove the synthesised voice actually reaches Zoom's microphone input.
#
# "paplay returned 0" is not proof - it only says a file played. Earlier today that
# same reasoning would have told me the voice worked while Zoom had no capture stream
# at all and every word went into the void.
#
# So this records FROM the exact source Zoom is capturing (vmic_src) while speaking
# INTO the sink that feeds it, and then measures the samples. A silent recording means
# the chain is broken no matter what any exit code says.
#
# It also records a SILENT control first, so a non-silent result cannot be blamed on
# the recorder picking up noise or on a broken amplitude check.
set -u

SRC=vmic_src
SINK=vmic

echo "=== control: record ${SRC} with NOTHING playing ==="
timeout 4 parec --device="${SRC}" --file-format=wav /tmp/silence.wav >/dev/null 2>&1
echo "  control bytes: $(stat -c %s /tmp/silence.wav 2>/dev/null || echo 0)"

echo "=== synthesise ==="
espeak-ng -v en-gb -s 140 -p 45 -w /tmp/voice.wav \
  "Testing the presenter voice. One. Two. Three. Four. Five." 2>/dev/null
echo "  speech bytes: $(stat -c %s /tmp/voice.wav 2>/dev/null || echo 0)"

echo "=== record ${SRC} WHILE speaking into ${SINK} ==="
timeout 9 parec --device="${SRC}" --file-format=wav /tmp/heard.wav >/dev/null 2>&1 &
REC=$!
sleep 1
paplay --device="${SINK}" /tmp/voice.wav >/dev/null 2>&1
wait ${REC} 2>/dev/null
echo "  heard bytes: $(stat -c %s /tmp/heard.wav 2>/dev/null || echo 0)"

echo "=== measure both, so silence is distinguishable from sound ==="
python3 /tmp/measure_wav.py /tmp/silence.wav /tmp/heard.wav
