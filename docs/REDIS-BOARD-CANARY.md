# Redis board requests: current safety boundary

**Status:** source supplement only. The shared board carries unauthenticated `Source_Tag` claims and shared `AYA_RESULT` rows. It cannot safely expose arbitrary Redis values or establish per-agent identity. The existing Redis ACL in `scripts/redis_acl.json` is a separate governance layer; its broad read rights do not authorize publishing a value to the board.

## Default-off canary

For unauthenticated board rows, the `bus_request` worker rejects every `redis-op` before a Redis run marker or operation unless both runtime settings are explicit:

- `REDIS_BOARD_CANARY_ENABLED=1`
- `REDIS_BOARD_CANARY_KEY=conf:canary:redis-op:<synthetic-id>`

The key must match the exact configured value and the restricted namespace grammar. Even then, only `set` of the fixed public marker `PUBLIC_CANARY_V1` and `get` of that same key are accepted. The request must be a canonical ten-cell row with one stable `Row_ID` equal to `id` and `req`; malformed or repeated fields, extra fields, other operations, and changed values are refused. A `get` result reports only `match=yes` or `match=no`; no value is copied to `AYA_RESULT`. The ordinary synthetic probe remains separate.

The existing Git comment route uses a separately authenticated author binding and retains its prior governed result contract. This supplement does not widen that route. Its replies still need a separate confidentiality review before private values are requested.

`Source_Tag` remains an unauthenticated claim. This restriction is safe only because the allowed value and key are synthetic and non-sensitive. It does not prove a Gemini, Grok, Cursor, or Claude runtime is active, nor does a posted request prove execution. Confirmed execution requires a correlated result and audit entry. A prior run claim without a result is `UNKNOWN` and must not be retried under a new ID automatically.

The model adapter's REDIS-OP feature is off for Gemini and Grok by default. If a reviewed test explicitly enables it, the adapter still enforces the exact canary rule and requires a durable posting claim. Laptop paths without that claim refuse. Incomplete model output, an ambiguous REDIS-OP line, or an uncertain append never becomes a success claim. Model access and costs remain governed by existing admission settings; this patch does not enable a model.

## Manual canary request contract

The CLI requires a caller-supplied stable `--id` so replay uses the same row identity. An example for **offline fixtures only**:

```text
python scripts/fleet_agent.py request set conf:canary:redis-op:test --val PUBLIC_CANARY_V1 --tag gemini --id REQ-TEST-0001
```

The worker runtime must have the matching explicit canary settings. The CLI's exact ten-column preflight and readback establish only that the request row landed, not that the worker accepted or executed it. `AYA_RESULT` is a shared row and reveals only status or marker match. Do not put private material in request rows.

## Rollback and live prerequisites

Keep the owner-reported production fallback `8912b2e`. This supplement is not deployed. Before any later approved canary rollout, review the exact worker and adapter revisions together, configure only a synthetic key, verify a running consumer and a separate audit/result observation path, and preserve the fallback for rollback. Generic private reads, per-agent identity, and a confidential return transport need separate design and approval.
