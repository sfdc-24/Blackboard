---
title: Fleet utilization metrics
id: FULL-STRATEGY-BUILD-20260927
owner: grok
updated_at: 2026-09-27T04:21:00Z
---

# Utilization metrics

Derived from Codex architecture discipline (evidence levels + promotion ledger) and overnight PM policy.

| ID | Metric | Source | Cadence | Owner |
|---|---|---|---|---|
| U1 | Agent busy ratio | board RESULT/ACK in 60m ÷ roster | 30m | grok |
| U2 | Green-merge throughput | squash-merges of density+test PRs | overnight | grok |
| U3 | Exact-head verdict lag | PR open → Codex GO/NO-GO | per PR | Codex (Test lead) |
| U4 | Cooking freshness | `bake.py` age | ≤30m when API up | copilot / baker |
| U5 | Doorbell→docs lag | NOTE that changes field → living doc commit | ≤15m | copilot |
| U6 | Gate debt | open NO-GO / pending heads on Cooking | continuous | Codex |
| U7 | Spike A proof | heard-call gate status | per spike | Claude CHAIR + Codex Test lead |
| U8 | Public Ops honesty | snapshot age on `/ops/` | 5m bake / 120s poll | site lane |

Public `/ops/` may show U2/U8-style improvement numbers. It must **not** show internal wiki jargon or secrets.
