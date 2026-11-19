"""Client for the gas-bus gateway's R-STATUS actions: status_write, status_setup.

Ruling, blackboard-5e 2026-11-14: new gas-bus/Code.js gateway actions, reusing
BUS_URL/BUS_SECRET - the same credential every other writer on this fleet
already holds. The gateway enforces the M-R-only boundary on its own side,
which is a stronger guarantee than a Sheets service account holding edit
rights on the whole sheet, and avoids an owner permission change (sharing the
sheet with a new account). No Sheets API credential lives in this client.

Injectable: every call takes a `fetch` function (default scripts/bus.fetch)
so tests run against a fake gateway, no network - same shape as every other
scripts/*_client module on this fleet.
"""
import json

from bus import fetch as _bus_fetch

TITLE = "Blackboard - Alpha DB"
MAX_ROWS_PER_CALL = 500   # matches gas-bus/Code.js's STATUS_WRITE_MAX_ROWS


class StatusClientError(Exception):
    """The gateway refused a status_write/status_setup call, or answered a
    shape this client does not understand."""


class StatusClient:
    def __init__(self, env, fetch=None, title=TITLE):
        self.env = env
        self.fetch = fetch or _bus_fetch
        self.title = title

    def write(self, updates):
        """updates: [{"row_id": str, "values": [6 strings, M..R]}, ...], in any
        order. Batches at MAX_ROWS_PER_CALL (the gateway refuses more per call).
        Returns {"written": n, "misses": [row_id, ...]} merged across batches -
        a miss is a row_id the gateway could not find in column A, never
        written anywhere, not a partial write."""
        if not updates:
            return {"written": 0, "misses": []}
        written, misses = 0, []
        for start in range(0, len(updates), MAX_ROWS_PER_CALL):
            batch = updates[start:start + MAX_ROWS_PER_CALL]
            data = self._call("status_write", {"updates": batch})
            written += int(data.get("written") or 0)
            misses.extend(data.get("misses") or [])
        return {"written": written, "misses": misses}

    def setup(self):
        """Idempotent one-time setup: the M1:R1 header names, data validation
        on M, the warning-only protected range on A:L, and the "Open asks"
        filter view (skipped, not failed, when the Advanced Sheets service
        is not enabled). Returns the gateway's own report dict."""
        return self._call("status_setup", {}).get("report", {})

    def _call(self, action, extra):
        body = dict(extra)
        body.update({"secret": self.env["BUS_SECRET"], "action": action, "title": self.title})
        raw = self.fetch(self.env["BUS_URL"], body)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise StatusClientError(
                "gateway answered non-JSON for %s: %s" % (action, str(raw)[:200])) from error
        if not isinstance(data, dict) or not data.get("ok"):
            raise StatusClientError(
                "gateway refused %s: %s" % (action, (data or {}).get("error") if isinstance(data, dict) else data))
        return data
