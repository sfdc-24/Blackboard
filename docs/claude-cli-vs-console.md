# Claude Code CLI vs Claude Console — SFDC24 blackboard + sfdc24.com

**Status:** standing protocol (2026-09-18 overnight evidence)  
**machineId:** `5b4ca3a5-66b3-4987-93f5-c1b6424dc1ea`  
**Paths:** blackboard `C:\\Users\\salam\\quantum\\blackboard` · site `C:\\Users\\salam\\Quantum\\sfdc24-site`  
**Cite:** `docs/board-protocol.md` · `docs/agent-reply-contract.md`  
**Evidence labels:** **TESTED** = observed tonight / this session · **BELIEVED** = reasoned, not instrumented

Prefer **Console / Anthropic Messages API** when the ask is text-only and speed matters; prefer **Claude Code CLI** (or `vm-claude-code-cli`) whenever git, Salesforce CLI, local bus scripts, or overnight filesystem ownership is required.

Full doc mirrored on sfdc24-site PR #83 and Drive: https://drive.google.com/file/d/1428cUI2wb8249hZSeyorVCdRHHNqkyQA/view

## Use cases (summary)

1. Board DISPATCH/RESULT — prefer **CLI** (both for ack)
2. Repo edit + PR — prefer **CLI**
3. Quick architecture YES/NO+ETA — prefer **Console**
4. Overnight multi-hour — prefer **CLI** (Console NO TESTED for SFDC-LEADS-UI)
5. SF org / VANLAS tools — prefer **CLI**
6. Speed/metrics — prefer **Console/API** (+ Foundry lane note)
7. Grok local→cloud migration — **both**, board-first

See PR #83 `docs/claude-cli-vs-console.md` for full protocol, cost table, and migration checklist.

## Board

DISPATCH `GROK-CLI-VS-CONSOLE-001` uuid `163d47d6-334d-4cbd-89a2-ba51b26a419b` → `claude-code-cli;console;foundry` — YES/NO+ETA ack for later migration.
