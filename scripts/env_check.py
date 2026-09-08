#!/usr/bin/env python3
"""Report presence/shape of the WhatsApp env keys WITHOUT printing values."""
from bus import load_env

env = load_env()
for k in ("WA_TOKEN", "META_TOKEN", "WA_PHONE_NUMBER_ID", "WA_TO", "WA_GRAPH_VERSION"):
    v = env.get(k)
    if v is None:
        print(f"{k:20} ABSENT")
    elif not v:
        print(f"{k:20} PRESENT but EMPTY")
    else:
        shape = "starts with 'Bearer '" if v.startswith("Bearer") else "raw value"
        # non-secret keys are safe to echo
        if k in ("WA_PHONE_NUMBER_ID", "WA_TO", "WA_GRAPH_VERSION"):
            print(f"{k:20} = {v}")
        else:
            print(f"{k:20} PRESENT, len={len(v)}, {shape}")
