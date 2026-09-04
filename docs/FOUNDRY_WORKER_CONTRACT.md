# Microsoft Foundry worker contract

Status: local governed seam, version 1. It does not grant Foundry direct access
to the Blackboard, Google Drive, Salesforce, Apps Script, Meta, or Pipedream.

The orchestrator constructs and validates a `work_packet.v1`, invokes Foundry,
validates the model's `work_assessment.v1`, and emits one canonical
`work_result.v1` containing that assessment plus adapter-owned provenance. The
orchestrator owns any subsequent routing or Blackboard write under its own
writer tag. Claude remains the trusted Salesforce executor. VM CLI/Claude
remains the CI/CD and infrastructure lead.

## Invoke

```powershell
python -B scripts/ask_foundry.py `
  --packet examples/foundry/work_packet.v1.example.json `
  --out result.json
```

`--file` is retained as an alias for `--packet`, but it now means a validated
UTF-8 `work_packet.v1` JSON file. `--prompt` remains available only for an
unstructured, model-deployment smoke test. Prompt mode rejects `--agent` and
`--agent-version`, never falls back to `FOUNDRY_AGENT_NAME`, and requires
`--model` or `FOUNDRY_MODEL`.

Governed packet mode defaults to `FOUNDRY_MODEL` and may fall back to
`FOUNDRY_AGENT_NAME`. Governed agent execution requires a version pin through
`--agent-version` or the optional local `FOUNDRY_AGENT_VERSION` setting.

Configuration remains local in `.foundry.env` first and `.env` second. Required
names are `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_API_KEY`; target selection uses
`FOUNDRY_MODEL` or `FOUNDRY_AGENT_NAME`. Never pass credential values as command
arguments or write them into packets.

## `work_packet.v1`

Exactly four top-level fields are accepted:

```json
{
  "schema": "work_packet.v1",
  "work_id": "BOARD-FIRST-ID",
  "task": "One bounded evaluation task.",
  "evidence": [
    {
      "source_id": "stable:source:reference",
      "retrieved_at": "2026-09-04T12:00:00Z",
      "sha256": "sha256 of the exact UTF-8 text value",
      "instruction_authority": "NONE",
      "text": "Evidence supplied by the orchestrator."
    }
  ]
}
```

Validation rules:

- packet file: at most 256 KiB;
- task: nonempty, at most 8,000 characters;
- evidence: at most 32 items;
- each evidence text: nonempty, at most 65,536 characters;
- combined evidence text: at most 200,000 characters;
- source IDs: unique and restricted to stable identifier characters;
- `retrieved_at`: valid ISO 8601 UTC ending in `Z`;
- `sha256`: lowercase digest of the exact UTF-8 `text` field;
- `instruction_authority`: exactly `NONE`;
- unknown and duplicate JSON fields are rejected.

An empty evidence array is valid so Foundry can return `BLOCKED` and identify
what evidence is required.

## Request controls

Governed execution always sends:

- fixed higher-priority instructions declaring evidence text untrusted data;
- `store: false`;
- a bounded `max_output_tokens` value;
- `metadata.work_id`, packet digest, and contract version;
- `tool_choice: none` and no external retrieval;
- truncation disabled so missing context cannot be silent;
- strict JSON Schema output;
- the exact agent version when agent mode is selected.

The unstructured model smoke mode also sends `store: false`, a bounded
`max_output_tokens`, `tool_choice: none`, `parallel_tool_calls: false`, and
disabled truncation. It cannot invoke a persisted agent or expose agent tools.

The project endpoint must use HTTPS, port 443, an expected Foundry Azure host,
and the project-scoped `/api/projects/<project>` path. URLs with credentials,
queries, fragments, alternate ports, or unrelated hosts are rejected before the
API key can be sent. HTTP redirects are not followed, preventing a credential
header from being forwarded to a second host.

## `work_assessment.v1` model output

The model must return `PASS`, `BLOCKED`, or `FAIL` with these fields:

```json
{
  "schema": "work_assessment.v1",
  "work_id": "BOARD-FIRST-ID",
  "status": "PASS",
  "findings": [
    {
      "claim": "A finding supported by supplied evidence.",
      "evidence_refs": ["stable:source:reference"],
      "confidence": "high"
    }
  ],
  "missing_evidence": [],
  "recommended_next_action": "Return this result to the orchestrator."
}
```

The adapter rejects invented source IDs and uncited findings. `PASS` requires at
least one cited finding and no missing evidence. `FAIL` also requires at least
one cited finding. `BLOCKED` requires at least one `missing_evidence` item and
may have no findings.

## `work_result.v1` downstream artifact

After validating `work_assessment.v1`, the adapter changes the schema identifier
to `work_result.v1` and adds the required `provenance` object. This is the only
artifact downstream workers consume:

```json
{
  "schema": "work_result.v1",
  "instruction_authority": "NONE",
  "work_id": "BOARD-FIRST-ID",
  "status": "PASS",
  "findings": [
    {
      "claim": "A finding supported by supplied evidence.",
      "evidence_refs": ["stable:source:reference"],
      "confidence": "high"
    }
  ],
  "missing_evidence": [],
  "recommended_next_action": "Return this result to the orchestrator.",
  "provenance": {
    "packet_sha256": "canonical packet digest",
    "response_id": "required Foundry response identifier",
    "response_status": "completed",
    "response_model": "required model reported by Foundry",
    "requested_target": {
      "type": "model_deployment or agent",
      "name": "requested deployment or agent",
      "version": "pinned agent version or null"
    },
    "reported_target": {
      "type": "response_model or agent",
      "name": "reported model or agent",
      "version": "reported agent version or null"
    },
    "usage": {
      "input_tokens": 0,
      "output_tokens": 0,
      "total_tokens": 0
    },
    "elapsed_ms": 0,
    "completed_at": "2026-09-04T12:00:00.000Z"
  }
}
```

`instruction_authority: "NONE"` is adapter-owned and applies to the entire
model-authored assessment: status, findings, missing-evidence statements, and
`recommended_next_action`. The result is advisory evidence only. No downstream
component may auto-execute its recommended action, change routing, call a tool,
or perform a system-of-record write based solely on this artifact. A separately
authorized orchestrator must validate and approve any subsequent action.

`provenance` is also never model-authored. Standard output and `--out` contain
the same exact-key enriched artifact. The canonical packet SHA-256, required
Foundry response ID and model, requested target and version, independently
reported target and version, token usage, elapsed milliseconds, and ISO UTC
completion timestamp are therefore available to every downstream validator.
Missing response identity or model fails with exit 6 before an artifact is
written. For agent runs, the reported agent name and version must exactly match
the pinned request or the result is rejected.

The result path may not be the same as the packet path, preventing an output
write from destroying its own input evidence.

All JSON artifacts and console records use deterministic key ordering and
ASCII JSON escaping. Plain smoke-test output escapes non-ASCII code points for
the console. This keeps strict Windows `cp1252` streams safe; when `--out` is
used, the artifact is written before the success output and receipt, and a
valid non-ASCII result cannot turn into an ambiguous nonzero exit merely because
the console cannot encode it.

## Errors and exit codes

Errors are JSON on standard error using `foundry_error.v1`; credential values
and remote response bodies are never included.

| Exit | Class | Meaning |
|---:|---|---|
| 0 | success | Valid result or discovery response |
| 2 | `USAGE` | Conflicting or invalid command options |
| 3 | `CONFIG` | Missing target/configuration or untrusted endpoint |
| 4 | `INPUT` / `CONTRACT` | Unreadable or invalid work packet |
| 5 | `REMOTE` | Authentication, authorization, rate, HTTP, or network failure |
| 6 | `RESULT` | Incomplete, empty, malformed, or unsupported Foundry result |
| 7 | `OUTPUT` | Result artifact could not be written |
| 130 | `INTERRUPTED` | Operator interrupted the invocation |
| 70 | `INTERNAL` | Unexpected adapter failure, with internals suppressed |

Retryability is explicit in the error artifact. HTTP response bodies are not
echoed because they can contain customer or tool data.
