#!/usr/bin/env python3
"""Report presence and shape of the env keys WITHOUT printing secret values.

Checks the BUS credentials first, because reaching the board is what a fresh
clone needs, then the WhatsApp keys. Read-only: it spends no board write and no
WhatsApp send quota.
"""
from bus import ENV_PATH, load_env

SECRET_KEYS = {"BUS_SECRET", "WA_TOKEN", "META_TOKEN"}
SAFE_TO_ECHO = {"WA_PHONE_NUMBER_ID", "WA_TO", "WA_GRAPH_VERSION"}
GROUPS = (
    ("bus (needed to reach the board)", ("BUS_URL", "BUS_SECRET")),
    ("whatsapp (only needed to send)", ("WA_TOKEN", "META_TOKEN", "WA_PHONE_NUMBER_ID", "WA_TO", "WA_GRAPH_VERSION")),
)


def describe(key, value):
    if value is None:
        return "ABSENT"
    if not value:
        return "PRESENT but EMPTY"
    if key in SAFE_TO_ECHO:
        return f"= {value}"
    if key == "BUS_URL":
        # The URL is not a secret, but the deployment id is noise here; show
        # only enough to confirm it is an Apps Script /exec endpoint.
        return f"PRESENT, len={len(value)}, ends /exec" if value.rstrip("/").endswith("/exec") else f"PRESENT, len={len(value)}, does NOT end in /exec"
    if key in SECRET_KEYS:
        shape = "starts with 'Bearer '" if value.startswith("Bearer") else "raw value"
        return f"PRESENT, len={len(value)}, {shape}"
    return f"PRESENT, len={len(value)}"


def main():
    env = load_env()
    print(f"env file: {ENV_PATH}\n")
    for title, keys in GROUPS:
        print(title)
        for key in keys:
            print(f"  {key:20} {describe(key, env.get(key))}")
        print()
    # load_env() already refuses when BUS_URL or BUS_SECRET is missing, so
    # reaching this line means the board credentials are at least present.
    print("bus credentials present. To prove they WORK, run:")
    print("  python scripts/board_summary.py 3")


if __name__ == "__main__":
    main()
