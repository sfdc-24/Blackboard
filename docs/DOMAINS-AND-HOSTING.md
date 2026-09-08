# Domains and hosting as an SFDC24 service — research

claude-code-cli, 2026-09-08. Asked for by Mr. Salam:

> do research and find out how we can incorporate domain lookup and registration
> as part of sfdc24.com service including hosting pages with CMS like wordpress
> or google site

**Status of everything below: READ, NOT DEMONSTRATED**, except the one part
marked LIVE. Nothing here has been wired up. Three of the findings change what is
worth building, and one of them kills a thing he named.

---

## The short version

| | finding | what it means |
|---|---|---|
| **Lookup** | Solved, cheap, no commitment | Ship it. Days of work, not weeks. |
| **Registration** | Technically easy, **commercially a decision only he can make** | Blocked on him, not on code. |
| **Google Sites** | **No usable API. Dead end.** | Cannot be automated. Ever, on current evidence. |
| **WordPress** | Only sane as Multisite **on a server we then have to run** | Real ongoing burden. Default to no. |
| **Custom domains on our current host** | **GitHub Pages allows one custom domain per site** | This is the actual blocker, and it is architectural. |

---

## 1. What already exists, and is LIVE

`tools/prototype_publisher.py` in the site repo publishes a static bundle to
`https://www.sfdc24.com/p/<opaque-work-id>/`, deterministically, with a content
digest, tests, and a CI workflow. Its own doc already draws the line he is asking
about:

> This is a publishing mechanism, not a registration or DNS mechanism.

So **the "hosting a page" half of his question is already answered and running**,
as long as the page lives under our domain. Everything below is about the half
that is not: the client's *own* domain, and a CMS they can edit themselves.

---

## 2. Domain lookup — easy, do it first

Two credible options, both with real availability + pricing APIs.

**NameSilo** — `checkRegisterAvailability`, simple key-in-URL API, sandbox access
by email request. Registration is paid from prepaid **account funds**; there is no
minimum deposit, and no card/PayPal at the API. Cheapest wholesale of the three
he named.

**Cloudflare Registrar API** — went to **beta on 2026-04-15**. Four endpoints:
*search* (generates candidate names from keywords), *check* (live availability and
price), *register*, *poll*. Explicitly aimed at scripted and agent-driven
workflows, at wholesale cost.

**Recommendation: build lookup against Cloudflare's `search` + `check`.** The
`search` endpoint turns a description into candidate names, which fits the beat-1
flow exactly — he describes the business, the page and the name appear together.
NameSilo has no equivalent generative search.

**Caveat worth stating plainly:** it is a **beta**. Renewals, transfers and
contact updates are *not yet* available through it. A service that can register a
domain but not renew it is not a service.

---

## 3. Registration — the decision is commercial, not technical

Both APIs register domains **into the operator's own account**. Cloudflare's docs
are explicit that you may override the registrant contact but this is *"not to
register on behalf of unrelated third parties"*. So there are three models and
they are genuinely different businesses:

**A · Client's own registrar account, we automate against their key.**
They own the asset. We never hold it, never carry the renewal, never appear in a
dispute. Costs: onboarding friction, and we would be handling a client's API
credential — which cuts straight across D-18. *Recommended if we do this at all.*

**B · We register, then push or transfer to them.**
No friction, instant. But we hold the asset, we carry the renewal liability, and
a client who leaves badly has a hostage problem. NameSilo supports account-to-
account push, which makes the exit clean *if we remember to do it*.

**C · We advise, they buy, we do the DNS.**
Zero liability, zero automation, zero revenue. Also zero risk of being the reason
someone's domain lapsed.

**This is his call and I have not made it.** My read: **C for now, A when a client
asks twice.** B is the one that looks easiest today and hurts in a year.

### .ca specifically

He asked about CIRA earlier. Clarified:

- We do **not** need to become a CIRA-certified registrar. Going through an
  accredited registrar's API (NameSilo, Namecheap, OpenSRS/Tucows, enom) is fine.
- Becoming one *would* require Canadian citizenship/residency and a Canadian place
  of business — he would personally qualify, but it is not worth it.
- **The registrant must satisfy Canadian Presence Requirements** — one of eighteen
  categories — and CIRA runs Registrant Information Validation and can demand
  proof. So a `.ca` cannot be parked in our name for a non-Canadian client. That
  is a compliance trap, not a technicality.

---

## 4. Hosting a client's own domain — the real blocker

**GitHub Pages allows one custom domain per repository/site.** Our site is one
Pages site bound to `www.sfdc24.com`. So we cannot serve `clientname.com` from it.
Not a configuration problem — a platform limit.

Options:

1. **One repo per client domain.** Works, scales badly, and multiplies the
   release-gate surface codex already polices. No.
2. **Cloudflare Pages.** Many custom domains per project. Registrar, DNS,
   certificates and hosting in one account and one API. *This is the natural
   move* and it pairs with the registrar finding above.
3. **Netlify / Vercel.** Same capability, another vendor, no reason to prefer them
   given we would already be at Cloudflare for the registrar.

**Do not read this as "migrate the site to Cloudflare."** `www.sfdc24.com` is
working, has a cert, and has already been broken once by a DNS change. See
`sfdc24-dns-shape`. The proposal is a *second* surface for client domains, with
the main site left alone until there is a reason.

---

## 5. The CMS question

### Google Sites — no

The Google Sites Data API is **deprecated and can only access classic Sites**, not
the version launched in November 2016 that everyone actually uses. There is no
supported way to create or edit a modern Google Site programmatically.

This matches what we already learned the hard way: Sites embeds steal focus, and
an Apps Script page can never hold a microphone, which is why `/voice/` exists as
a real page. **Google Sites cannot be part of an automated service.** If a client
already has one, we work around it; we do not build on it.

### WordPress — only if the client will actually edit it

The only automatable shape is **WordPress Multisite** with `wp site create` over
WP-CLI, on a host we control, optionally with a WaaS plugin layer for signup and
domain mapping.

Read that as what it is: **a server we must run, patch, back up and defend.**
WordPress is the most-attacked software on the web, and this is a one-person
consultancy whose credibility rests on not shipping things that quietly fail. A
compromised client site hosted by us is worse than never offering hosting.

**Recommendation: do not host WordPress.** When a client needs a CMS, provision it
*on their hosting* and hand it over, or point them at a managed host. Keep the
static publisher as the default; it has no attack surface, no patch cycle, and it
is already live.

---

## 6. What is realistic by September 10

**Yes, achievable:**
- Domain lookup in the intake/beat-1 flow: he describes the business, we show
  available names with real prices. Read-only, no money, no liability.
- Saying honestly what we can host today: `sfdc24.com/p/<id>/`, live now.

**No, not by the 10th, and I will not pretend otherwise:**
- One-click registration for a client. The blocker is the ownership decision, not
  the code, and it should not be rushed.
- Any WordPress hosting.
- Custom client domains — that needs the Cloudflare Pages surface built and
  proven first.

---

## What I need from him

1. **Ownership model: A, B or C above.** Everything about registration waits on
   this one answer.
2. **Is a second host (Cloudflare Pages) for client domains worth opening**, or do
   client prototypes stay under `sfdc24.com/p/...` for now?
3. **Confirm WordPress hosting is out**, or tell me why the risk is worth it —
   he has run more of these engagements than I have and may know something I do
   not.

## Sources

- [NameSilo API Manager](https://www.namesilo.com/support/v2/articles/account-options/api-manager) ·
  [Account Funds](https://www.namesilo.com/support/v2/articles/account-options/account-funds-manager)
- [Cloudflare Registrar API docs](https://developers.cloudflare.com/registrar/registrar-api/) ·
  [beta announcement, 2026-04-15](https://blog.cloudflare.com/registrar-api-beta/)
- [CIRA Canadian Presence Requirements — registrants](https://www.cira.ca/en/resources/documents/domains/canadian-presence-requirements-registrants/) ·
  [registrars](https://www.cira.ca/en/resources/documents/domains/canadian-presence-requirements-registrars/)
- [Google Sites Data API overview (deprecated, classic only)](https://developers.google.com/workspace/sites/docs/developers_guide)
- [GitHub Pages — managing a custom domain](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site)
- [WP-CLI `wp core multisite-install`](https://developer.wordpress.org/cli/commands/core/multisite-install/)
- `sfdc24-site: docs/PROTOTYPE-PUBLISHER.md` (ours, live)
