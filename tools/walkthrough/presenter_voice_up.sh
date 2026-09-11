#!/bin/bash
# Give the presenter box a microphone it does not physically have, so it can SPEAK
# into a Zoom meeting rather than presenting in silence.
#
# WHY THIS EXISTS
#   The rig could share a screen but had no voice. Mr Salam, live in the meeting on
#   2026-09-11: "I dont hear any audio; this is something that you will have to figure
#   out how to speak in zoom."
#
# WHAT THE BOX ACTUALLY HAS
#   Nothing. No sound card, no microphone, no speakers. PulseAudio therefore starts a
#   dummy 'auto_null' sink, and Zoom - finding no input device at all - opens NO
#   capture stream. Every word spoken into the machine went into the void, and the
#   only way to notice was that `pactl list short source-outputs` was EMPTY while
#   paplay was cheerfully returning 0.
#
# THE CHAIN THIS BUILDS
#
#     espeak-ng  ->  sink 'vmic'  ->  vmic.monitor  ->  remap  ->  source 'vmic_src'
#                                                                        |
#                                                            Zoom captures this one
#
#   The remap step is the part that is easy to skip and cannot be skipped: Zoom does
#   not offer a bare .monitor as a microphone. module-remap-source turns it into a
#   first-class source that Zoom lists and selects, shown as "SFDC24-VirtualMic".
#
# AND THE SEPARATE SPEAKER, WHICH IS NOT OPTIONAL
#   Zoom's OUTPUT must not land in 'vmic', or Zoom's own audio re-enters Zoom's own
#   microphone and every other participant hears themselves echoed. Measured: with
#   Zoom's speaker on vmic, a recording taken with NOTHING playing had peak 1239.
#   After moving Zoom's output to 'zspk', the same control read peak 0. That is the
#   difference between a working meeting and an unusable one.
#
# IDEMPOTENT. Safe to run twice; existing modules are detected and left alone.
# NO CREDENTIAL. This does not join a meeting and does not want the join URL.
set -uo pipefail

have_module() {               # module name, then a string that must appear in its args
  pactl list short modules 2>/dev/null | grep -q "$1.*$2"
}

echo "=== presenter voice up $(date -u +%FT%TZ) ==="

if ! command -v espeak-ng >/dev/null 2>&1; then
    echo "  espeak-ng MISSING - install it first: sudo apt-get install -y espeak-ng"
    echo "  refusing to continue, because a voice path with no synthesiser is not a voice path."
    exit 1
fi
echo "  espeak-ng   $(command -v espeak-ng)"

# 1. The sink whose monitor becomes the microphone.
if have_module module-null-sink "sink_name=vmic"; then
    echo "  vmic sink   already present"
else
    pactl load-module module-null-sink \
        sink_name=vmic \
        sink_properties=device.description=SFDC24-VirtualMic-Sink >/dev/null
    echo "  vmic sink   created"
fi

# 2. The remap that makes it visible to Zoom as an input device.
if have_module module-remap-source "source_name=vmic_src"; then
    echo "  vmic_src    already present"
else
    pactl load-module module-remap-source \
        master=vmic.monitor \
        source_name=vmic_src \
        source_properties=device.description=SFDC24-VirtualMic >/dev/null
    echo "  vmic_src    created"
fi

# 3. A separate sink for Zoom's own output, so it never loops into the mic.
if have_module module-null-sink "sink_name=zspk"; then
    echo "  zspk sink   already present"
else
    pactl load-module module-null-sink \
        sink_name=zspk \
        sink_properties=device.description=SFDC24-Speaker >/dev/null
    echo "  zspk sink   created"
fi

pactl set-default-source vmic_src 2>/dev/null
pactl set-default-sink zspk 2>/dev/null

echo
echo "--- state, as returned values rather than assurances ---"
echo "  sources:"
pactl list short sources 2>/dev/null | sed 's/^/    /'
echo "  default source : $(pactl info 2>/dev/null | sed -n 's/^Default Source: //p')"
echo "  default sink   : $(pactl info 2>/dev/null | sed -n 's/^Default Sink: //p')"
echo "  capture streams open (Zoom reading a mic): $(pactl list short source-outputs 2>/dev/null | grep -c .)"

echo
echo "STILL TO DO BY HAND, INSIDE ZOOM:"
echo "  Zoom does not re-read the default device for a meeting it has already joined."
echo "  Click the chevron beside the Audio button and choose:"
echo "      Microphone -> SFDC24-VirtualMic"
echo "      Speaker    -> SFDC24-Speaker     (NOT SFDC24-VirtualMic-Sink, that echoes)"
echo "  Then confirm with tools/walkthrough/prove_voice.sh - a capture-stream count of"
echo "  zero means the meeting hears NOTHING no matter what any exit code says."
