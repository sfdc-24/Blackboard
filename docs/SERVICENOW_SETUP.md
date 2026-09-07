# ServiceNow — the same shape as the Salesforce lane, minus the trap we already hit

Written 2026-09-05 by `claude-code-cli`, after the Salesforce lane
(`SFDC-BACKEND-001`) stalled twice on the same cause. Every vendor claim below
was checked against a primary or vendor-published source this session; each is
marked **VERIFIED**, **VENDOR-STATED** or **BELIEVED**. Nothing here has been
run against a live instance yet — there is no instance yet. That is the point of
the doc.

---

## The one-paragraph answer

**We should not build a ServiceNow "playground".** The Salesforce Headless 360
Playground is a vendor-hosted LLM chat wired into the org, and it died twice in
one evening because its org token lives in a browser session
(`docs/HANDOVER.md`, board rows `4e366f15` and the 23:22Z halt). ServiceNow's
equivalent — **MCP Server Console** — is not merely worse for us, it is
**unavailable at the free tier**: it depends on the Now Assist Admin Console,
and Now Assist is not enabled on Personal Developer Instances at all. So the
free ServiceNow path *is* the headless path. We get on day one the durable rail
that vm-chrome concluded the Salesforce lane still needs.

That is a better starting position than Salesforce gave us, not a worse one.

---

## The mapping, piece by piece

| What we did on Salesforce | ServiceNow counterpart | Status |
|---|---|---|
| Free **Developer Edition org**, no expiry (`00Dbm00000wK2ibEAC`) | **Personal Developer Instance (PDI)**, free | Direct equivalent — **but a harsher lifecycle, see the trap below** |
| **Headless 360 Playground** — vendor-hosted chat + 21 MCP tools into the org | **MCP Server Console** (`sn_mcp_server` plugin) | **BLOCKED at our tier.** Needs Now Assist Pro Plus / Enterprise Plus |
| `sf` CLI + JWT — the durable rail vm-chrome said we still need | **REST Table API** + OAuth 2.0 (or basic auth w/ an extra role) | **Available on a PDI on day one** |
| `soqlQuery` read-back after every write (D-4) | Table API `GET` with `sysparm_query` | Same discipline, same rule |
| Web-to-Lead form on sfdc24.com | Inbound REST or a Service Catalog record producer | Available; more flexible than Web-to-Lead |
| Case + `Strategy__c`/`Category__c`/`Topic__c`/`Tag__c` taxonomy | `sn_customerservice_case` or `incident` + custom `u_` columns | Available; a PDI **can** create custom tables and fields, which the Playground could not |

Note the last row. The Salesforce lane burned an evening on the fact that the
Playground **cannot create custom objects or record types**, and that FLS is a
permission-set step no instance may click. A PDI hands the admin role to us
directly, so that entire class of blocker does not exist here.

---

## The trap — read this before provisioning anything

**A PDI is reclaimed when BOTH conditions hold** (VENDOR-STATED, policy
effective 2026-07-11):

1. the instance was provisioned **90 or more days** ago, **and**
2. there has been **no direct user login in the previous 10 days**.

The part that will bite us:

> **API calls, integrations and scheduled jobs do not count as activity.**
> Only a human logging directly into that specific PDI resets the clock.
> Visiting the developer portal does not count. Logging into a *different* PDI
> does not count.

So our agent traffic will keep the instance *useful* and will not keep it
*alive*. Ninety days in, a ten-day gap in Mr. Salam's own logins deletes the
instance and everything built in it.

This is the ISS-006 pattern exactly — a lifecycle rule nobody read, surfacing as
an outage. Two mitigations, both cheap, both to be done **before** we build
anything worth losing:

- **Treat the PDI as disposable.** Everything we create goes into a named
  **Update Set** and gets exported to XML in this repo, so a reclaimed instance
  is a re-provision plus an import, not a loss. This is the ServiceNow analogue
  of keeping reviewed source in its canonical repository. The public website's
  canonical repository is `sfdc-24/sfdc24-site`; Blackboard's `site/` is not a
  complete deployment tree (see `docs/HANDOVER.md`).
- **Watch the clock, not the instance.** `monitorTick` already runs every 15
  minutes on Google infrastructure and already emails on state change with a
  12/day cap (`docs/HANDOVER.md`). Add a day-count check that emails at day 7 of
  the 10-day window. Do not try to defeat the policy with a synthetic login — it
  is a login *policy*, and automating around it risks the account rather than
  the instance.

---

## Provisioning — the part only Mr. Salam can do

Signing up creates an account in his name and takes his credentials, so no
instance does this.

1. `https://developer.servicenow.com` → **Sign up** (free), then
   **Request Instance**.
2. Take the **Australia** release if offered a choice — it is the current family
   as of September 2026 (GA 2026-05-05; Brazil is next). Nothing we need is
   version-sensitive; the Table API has been stable since long before Tokyo.
3. Record three things and **only** these three: the instance URL
   (`https://devNNNNN.service-now.com`), the admin username, and the admin
   password. They go straight into `.env` on this laptop under D-18 — never into
   Drive, never into a board row, never into an agent chat.

Time: about ten minutes, most of it waiting for the instance to build.

The three lines go into `.env` on this laptop:

```
SNOW_INSTANCE=https://devNNNNN.service-now.com
SNOW_USER=<integration user, web-service-access-only, NOT the admin>
SNOW_PASS=<its password>

SNOW_CLIENT_ID=          # optional; both present switches snow.ps1 to OAuth
SNOW_CLIENT_SECRET=
```

They are duplicated here on purpose. `.env.example` is matched by `.gitignore`'s
`.env.*` rule, so the template is **not** in the repository and a fresh clone —
AkatiaVM, say — never sees it. That is the same trap as `gas/` being gitignored:
the error message points at a file the machine does not have. This doc is
tracked, so this block is the durable copy. (Not fixing the ignore rule here
deliberately: `.gitignore` is the single file PR #1 was conflicting on, and
vm-cli is unblocking that chain right now.)

---

## Wiring the rail — what an instance can do without a human

Done once, inside the instance, after it exists. All of this sits within the
admin role a PDI already grants: no entitlement, no purchase, no support ticket.

**Create a dedicated integration user rather than using the admin account.** The
admin account is Mr. Salam's; the rail should have its own identity so its
writes are attributable and its blast radius is bounded. `sys_user` → new user,
check **Web service access only**, then grant:

| Role | Why |
|---|---|
| `snc_platform_rest_api_access` | first gate on the Table API — without it every call is 401 regardless of ACLs (VENDOR-STATED) |
| `snc_basic_auth_api_access` | required if we authenticate with basic auth; PDIs restrict it by default (VENDOR-STATED) |
| `itil` *(or narrower)* | read/write on the task tables we actually use — grant the least that works, then read back |

**Prefer OAuth once the rail is proven.** `oauth_entity` → new **Application
Registry** entry, "Create an OAuth API endpoint for external clients". That
yields a client id and secret and gets us off long-lived passwords. Basic auth
is fine for the first end-to-end proof and nothing more — treat it the way we
treat `@HEAD`: a dev path, not the production one.

One vendor-stated detail worth writing down now, because it costs an hour to
rediscover: if we ever *do* reach the official MCP path on an entitled instance,
the OAuth **Token Format must be set to JWT**. Left on the default `Opaque`, the
connection fails at auth time with nothing useful in the error.

**Prove it before building on it.** The ServiceNow equivalent of D-4:

```
GET /api/now/table/sys_user?sysparm_query=user_name=<integration user>&sysparm_limit=1
```

A 200 carrying exactly one record means auth, role and ACL all hold. A 401 means
the role gate. A 200 with `{"result":[]}` means the ACLs are silently filtering —
which is the failure that looks like success, so **check the record count, not
the status code**. That is the same lesson as ISS-015: a 200 is not a working
system.

---

## The client

`scripts/snow.ps1` is written and follows the `bus.ps1` contract exactly:
credentials are read from `.env` at run time and **never appear on a command
line**, because this laptop's permission classifier blocks any shell command
carrying a secret inline (and D-18 says they belong in the env file anyway).

It is **read-only unless `-Write` is passed**, which is the opposite default to
the community ServiceNow MCP servers — those ship with destructive writes
enabled. Given that the entire point of this system is an auditable trail, an
accidental `DELETE` from a mis-parsed instruction is the failure we cannot
afford.

**It has never been run against a live instance**, because there is no instance.
It says so in its own header, the same way `public_inbox_quarantine.gs` sat
inert until it could be deployed and verified. The first `-Action whoami` after
`.env` is filled in is the test.

---

## What this is for — the business case, briefly

The Salesforce work aims at running a customer's business off sfdc24.com with us
as lifetime success partner (`SFDC-BACKEND-001`, vm-cli's brief). ServiceNow is
not a second copy of that. It is a **different buyer**: ITSM/CSM shops who
already have an instance, already carry an audit obligation, and cannot safely
get their own agents anywhere near it.

That maps onto the thing the round-table already identified as the real product
and which `docs/PRODUCT.md` has not yet been pointed at: **make the audit trail
the product**. A governed, read-back-verified, quarantined agent rail into a
ServiceNow instance is something a regulated buyer wants and cannot get from a
community MCP server that defaults to destructive writes.

That is a claim about demand, and demand claims from this fleet have been wrong
before. It is **BELIEVED**, not verified, and it should meet a real buyer before
it earns another hour of build.

---

## Sources checked this session

- ServiceNow developer advocate blog, *Building an MCP Server on ServiceNow and
  Connecting Claude to It* — `sn_mcp_server` requires `sn_nowassist_admin`;
  OAuth token format must be JWT, not the default Opaque.
- ServiceNow Community, *What's New in MCP Server Console* (v1.4) — requires
  Zurich Patch 9 / Australia Patch 2 **and Now Assist for ITSM, HRSD or CRM**.
- ServiceNow Community, *Now Assist FAQs* and the solved thread *is Now Assist
  available for PDI?* — not enabled on PDIs; lab instances are the sanctioned
  route. **Community-sourced, not a ServiceNow-employee statement** — worth one
  confirming look at the instance itself once we have one.
- ServiceNow Community, *PDI Reclamation Rules* — the 90-day + 10-day policy
  effective 2026-07-11, and the explicit statement that jobs and integrations do
  not substitute for a login.
- ServiceNow Community release schedule — Australia current as of August 2026,
  Brazil next.
