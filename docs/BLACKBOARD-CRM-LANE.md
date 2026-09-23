# Blackboard works the pipeline — leads and opportunities in our own org

> **STATUS: on the build list. Not started. Nothing here authorises a write to
> any org.** This is the scope note that gets it onto the list with its
> prerequisite named, not a design.

Asked for by Mr Salam, **2026-09-23**: *"add it to list of things to build as
part of blackboard, it should be able to communicate with salesforce dev org and
manage leads actively and work on opportunities"*.

It replaces the standing ask that he resolve the lead org by hand. He is right
that the answer is a capability rather than a chore — but the chore is still the
prerequisite, and that is the first section for a reason.

## Not the same thing as `SALESFORCE-MCP-PLAN.md`

That document is about **inspecting and scoring a client's org**, it is under
`state: HOLD`, and it authorises nothing. This is different work and should not
inherit that hold:

|  | that plan | this lane |
|---|---|---|
| whose org | a **client's** | **ours**, the controlled dev org |
| what it does | reads, scores, reports | **reads and writes** CRM records |
| the product | a score a client pays for | the fleet doing its own sales admin |
| risk | client data, client trust | our own pipeline only |

They share a transport and nothing else. Keeping them separate is deliberate:
a write path into our own org must not become a precedent for a write path into
someone else's.

## The prerequisite, and only he can clear it

**The leads are not where we could work them.** The site's Web-to-Lead posts to
`oid 00Dbm00000wK2ibEAC` — a **third** org that no agent on this fleet can
query. PlaygroundOrg holds 22 Leads and **zero** with `LeadSource = sfdc24.com`.
So the number of site leads that ever arrived is **UNKNOWN, not zero**, and has
been since it was first measured on 2026-09-19.

A lane that actively manages leads, pointed at an org the leads do not land in,
manages an empty pipeline convincingly. **Nothing else here is worth starting
until that org is identified**, and the answer is one of:

1. that `oid` belongs to an org we already have credentials for, and nobody
   checked — cheapest outcome, and it should be ruled out first;
2. it is a real separate org and we get access to it;
3. it is abandoned, and the site's Web-to-Lead target is re-pointed at the dev
   org — which is a change to a live form and therefore his call.

## What already exists, so this is not greenfield

- **The Salesforce MCP client is live** — 21 tools across three servers, running
  as System Administrator against the controlled dev org. The transport is not
  the missing piece.
- **The dev org is a controlled DE org**, not a client org, which is exactly
  the right blast radius for a first write path.
- **The board already carries the work** — a lane that claims, reports and reads
  back is the pattern every other agent here follows, and the D-4 rule (a
  read-back is the only proof of a write) applies to a CRM write exactly as it
  applies to a board append.

## What "actively manage leads and work on opportunities" decomposes into

Ordered by how much it can go wrong, cheapest and safest first. Each is a
separate piece of work with its own review; this list is the scope, not a plan.

**Read-only first, and it earns its keep immediately**
1. A pipeline read the fleet can post: open Leads by age and source, Opportunities
   by stage, anything that has not moved in N days. This alone answers "what is
   in the pipeline" without a single write.
2. Reconcile the site against the org: every visitor who left an email on
   sfdc24.com versus every Lead with `LeadSource = sfdc24.com`. **That
   reconciliation is what would have caught the invisible org on day one.**

**Writes, smallest blast radius first**
3. Create a Lead from a site enquiry, with the Row_ID of the board row that
   caused it written onto the record, so every write is traceable back to its
   cause.
4. Log activity: a Task against the Lead when the fleet answers an enquiry.
5. Advance an Opportunity stage — **behind a human gate**. Stage is a forecast
   number; an agent moving it changes what the business believes about its own
   revenue. This is the first item on the list that should require his click,
   and it should stay that way until it has a track record.

**Never, without a separate ruling**
- Deleting or merging records.
- Anything in an org that is not ours.
- Mass operations. A loop that touches every Lead is how one bad predicate
  becomes a data incident.

## Open questions for him, when the prerequisite is cleared

1. **Which org is the system of record for site leads** — the dev org, or the
   unknown one, or a real production org we have not discussed?
2. **What may an agent change without asking?** My proposal: create and log
   freely, never advance a stage or edit an amount without a click.
3. **Does a Lead created by an agent look different from one a person created?**
   It should — a source field or a record type — so the pipeline can always be
   read without the fleet flattering it.

Related: `docs/SALESFORCE-MCP-PLAN.md` (held, different scope),
`docs/board-protocol.md` (D-4 read-back).
