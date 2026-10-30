# SFDC24 price list - DRAFT for the owner's review (2026-09-26)

**Status:** DRAFT. Nothing live reads it.
- `STUDIO_PRICE_TABLE` stays empty and `STUDIO_ENABLE_CHARTER` stays false.
- These numbers are a starting point for the owner to change, not prices anyone has agreed to.
- Prices are the owner's decision (BLK-004, Blackboard #268's payment-link ruling).

**Machine-readable form:** `docs/SFDC24-PRICE-TABLE-DRAFT-20260926.json`, in the shape `app/pricing.py` accepts:
- `{currency, lines, support}`, keyed by homepage topic and quote line id;
- it parses cleanly with `parse_price_table`.

**Currency:** CAD. The market is Toronto small businesses.

**Where the ranges come from:** general market knowledge of Toronto and Canadian freelance and small-agency pricing.
- They are not quotes collected from named competitors.
- No competitor price was looked up or verified for this draft.

## How a quote is built

Each topic's quote has fixed lines (`workers/topics.py` `QUOTE_LINES`). The table prices each line, and the PDF shows
a total only when every line has a price. Payment is split 50% to start build and test and 50% on delivery and
handover. Support is chosen at handover.

**Tiers:**
- The price table holds **one price per topic line**, so a topic can carry only one tier.
- The JSON uses the tier marked **(in JSON)** below.
- Showing several tiers in one quote (for example, picked from the charter's "Site type") needs a small schema extension. That is a decision for the owner and a Codex review.

## Websites (topic `website`)

| Line | Company page, 5-7 pages **(in JSON)** | Blog / content site | E-commerce shop |
|---|---|---|---|
| Discovery and plan | 600 | 700 | 900 |
| Design | 1,200 | 1,400 | 1,800 |
| Build | 2,400 | 3,200 | 5,200 |
| Test and launch | 500 | 600 | 900 |
| Handover | 300 | 400 | 500 |
| **Total** | **5,000** | **6,300** | **9,300** |

**Why:**
- **Company page:** a small custom brochure site in Toronto commonly lands around CAD 3,000-8,000 from independents and small studios. 5,000 sits mid-range for a fast, live-prototyped build.
- **Blog / content site:** adds templates and a CMS, and training for posting.
- **E-commerce shop:** adds catalogue, cart, payment and pickup or shipping rules. Small shops commonly run CAD 7,000-15,000.

**Which numbers the samples use:**
- The JSON holds the **company-page** column: CAD 5,000 for the website lines. That is what a live price table would carry.
- The rendered e-commerce sample PDFs (a bakery shop) substitute this **e-commerce** column (CAD 9,300) for the website lines, for illustration only.
- Support prices in the samples come from the JSON: 250 a month and 150 an hour.

**Not included:** paid themes or plugins, hosting, domains, payment-processor fees, and product photography. These are passed through at cost, or quoted separately.

## Logo and brand kit (topic `logo`, default lines)

| Line | Price **(in JSON)** | What it covers |
|---|---|---|
| Discovery and plan | 300 | The brand, audience and references |
| Design | 900 | Three concepts, two refinement rounds |
| Build | 450 | Final files: primary, secondary and icon marks; palette; type pairing |
| Test and launch | 150 | Checks at small sizes, on dark and light, and in print |
| Handover | 200 | A one-page brand guide and source files |
| **Total** | **2,000** | Small-business logo packages commonly run CAD 1,000-4,000 |

## App prototype (topic `app`, default lines)

| Line | Price **(in JSON)** | What it covers |
|---|---|---|
| Discovery and plan | 1,200 | Users, flows and the data behind them |
| Design | 2,400 | Screen designs for the core flows |
| Build | 4,800 | A clickable, working prototype of the core flows. **Not a production app.** |
| Test and launch | 900 | Usability pass on phones, fixes |
| Handover | 500 | Walkthrough and documentation |
| **Total** | **9,800** | A production build is quoted separately, after the prototype |

## Salesforce (topics `salesforce_admin` and `salesforce_data`)

**Anchor rate:** CAD 150 an hour, about 66 hours per standard package. Independent Salesforce admins and consultants in
Canada commonly bill CAD 100-200 an hour; partner agencies bill more.

| Admin line | Price **(in JSON)** | | Data line | Price **(in JSON)** |
|---|---|---|---|---|
| Discovery | 1,200 | | Discovery | 1,200 |
| Configuration | 2,400 | | Data model | 1,800 |
| Automation | 3,000 | | Data quality and migration | 3,000 |
| Data | 1,500 | | Reports and dashboards | 2,100 |
| Testing | 900 | | Testing | 900 |
| Training and handover | 900 | | Training and handover | 900 |
| **Total** | **9,900** | | **Total** | **9,900** |

Small fixes below a package are handled as on-demand support, below.

## Something else (topic `other`, default lines)

| Discovery | Design | Build | Test and launch | Handover | **Total** |
|---|---|---|---|---|---|
| 500 | 800 | 1,600 | 400 | 300 | **3,600** |

This is a starting point for a small, loosely scoped piece of work. A larger scope is re-quoted after discovery.

## Support plans (all topics)

| Plan | Price | What it covers |
|---|---|---|
| **Subscription: Essentials (in JSON)** | 250 / month | Up to 2 hours a month of updates, fixes and questions; next-business-day response |
| Subscription: Growth | 600 / month | Up to 5 hours a month; priority response |
| Subscription: Priority | 1,200 / month | Up to 10 hours a month; same-day response on business days |
| **On demand (in JSON)** | 150 / hour | Billed in half-hour steps, with a one-hour minimum per request |

The JSON can hold one subscription price and one on-demand rate, so it holds Essentials. Offering all three tiers is the same schema decision as the website tiers.

## Decisions for the owner

1. **Tax.** Ontario HST is 13%. It applies only if SFDC24 is registered for HST.
   - Under the CRA small-supplier rule, registration becomes required once taxable supplies exceed CAD 30,000 **in a single calendar quarter**, or in total over the last four consecutive calendar quarters. Below that it is optional.
   - The PDF currently says "Applicable taxes are added on the invoice." and adds no tax line.
   - Should quotes show HST as its own line? This is general information, not tax advice; the owner's accountant confirms it.
2. **Currency.** CAD only, or CAD and USD? The table supports both, one per deployment.
3. **Rush fees.** For example, +25% for delivery in under two weeks. Nothing like this is in the table today.
4. **Discounts.** Charities or non-profits, repeat clients, or bundles (for example, a logo with a website). None is in the table today.
5. **Tiers.** Keep one tier per topic (the JSON as drafted), or extend the table so a quote can pick the tier from the charter.
6. **Validity and deposit.** Quotes are valid for 30 days; payment is 50% to start and 50% on delivery. Keep both?
7. **Seller identity and payment provider.** These must be settled before any live quote goes out (Blackboard #268, and the Converspan readiness plan in Blackboard #273).
