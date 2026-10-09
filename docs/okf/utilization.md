---
title: Fleet utilization metrics
id: FULL-STRATEGY-BUILD-20260927
owner: grok
updated_at: 2026-09-28T13:20:00Z
---

# Utilization metrics

Derived from Codex architecture discipline (evidence levels + promotion ledger) and overnight PM policy.

| ID | Metric | Source | Cadence | Owner |
|---|---|---|---|---|
| U1 | Agent busy ratio | bus RESULT/DONE in 60m ÷ roster | 30m | Grok Bot (delivery lead) |
| U2 | Green-merge throughput | squash-merges of density+test PRs | overnight | Grok Bot (delivery lead) |
| U3 | Exact-head verdict lag | PR open → Codex GO/NO-GO | per PR | Codex (quality and test lead) |
| U4 | Cooking freshness | `bake.py` age | ≤30m when API up | Copilot Agents / baker |
| U5 | Doorbell→docs lag | DISPATCH or RESULT that changes the field → living doc commit | ≤15m | Copilot Agents |
| U6 | Gate debt | open NO-GO / pending heads on Cooking | continuous | Codex (quality and test lead) |
| U7 | Spike A proof | heard-call gate status | per spike | Claude (data and security) + Codex |
| U8 | Public Ops honesty | snapshot age on `/ops/` (hub, roles, release NOW) | 5m bake / 120s poll | Gemini (admin) + site lane |

Public `/ops/` may show U2/U8-style improvement numbers, the fleet roles, and release NOW. It must use plain words: no internal wiki jargon, and no secrets.
