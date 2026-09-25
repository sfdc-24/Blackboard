"""The quote's price table - the owner's numbers, never ours.

The build plan and quote (app/summary_pdf.py) prices its line items and the
support plan options only from STUDIO_PRICE_TABLE. Prices are the owner's
decision (BLK-004 is open), so none is invented: a line without a price says
"Priced after review", and a total appears only when every line is priced.

    {"currency": "CAD",
     "lines": {"website": {"discovery": 800, "design": 1200, "build": 2400,
                           "test": 600, "handover": 400}},
     "support": {"subscription": 150, "on_demand": 120}}

currency is CAD or USD; "lines" is keyed by homepage topic, and each topic's
keys are its quote line ids (workers/topics.py quote_lines); "support" prices
the subscription per month and on-demand support per hour. Either may be left
out, not both. Every amount is a number from 1 to 1,000,000 with at most two
decimals. Anything else refuses the whole table.
"""
from __future__ import annotations

import json
import math

CURRENCIES = ("CAD", "USD")
SUPPORT = ("subscription", "on_demand")
AMOUNT_MAX = 1_000_000


def _amount(value) -> float:
    if (type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= AMOUNT_MAX
            or round(value, 2) != value):
        raise ValueError("a STUDIO_PRICE_TABLE amount must be 1 to %d with at most two decimals" % AMOUNT_MAX)
    return float(value)


def parse_price_table(raw, topics, line_ids) -> dict | None:
    """None for an empty setting; the checked table; ValueError for anything else.
    `line_ids(topic)` names the quote line ids a topic may price."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        table = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("STUDIO_PRICE_TABLE is not JSON") from exc
    if (not isinstance(table, dict) or "currency" not in table
            or not set(table) <= {"currency", "lines", "support"} or set(table) == {"currency"}):
        raise ValueError("STUDIO_PRICE_TABLE must be {currency, lines and/or support}")
    if table["currency"] not in CURRENCIES:
        raise ValueError("STUDIO_PRICE_TABLE currency must be one of %s" % ", ".join(CURRENCIES))
    out = {"currency": table["currency"], "lines": {}, "support": {}}
    if "lines" in table:
        lines = table["lines"]
        if not isinstance(lines, dict) or not lines:
            raise ValueError("STUDIO_PRICE_TABLE lines must be a non-empty object")
        for topic, prices in lines.items():
            if topic not in topics:
                raise ValueError("STUDIO_PRICE_TABLE lines names an unknown topic")
            allowed = set(line_ids(topic))
            if not isinstance(prices, dict) or not prices or not set(prices) <= allowed:
                raise ValueError("STUDIO_PRICE_TABLE lines for a topic must price only its quote lines")
            out["lines"][topic] = {line: _amount(amount) for line, amount in prices.items()}
    if "support" in table:
        support = table["support"]
        if not isinstance(support, dict) or not support or not set(support) <= set(SUPPORT):
            raise ValueError("STUDIO_PRICE_TABLE support must price subscription and/or on_demand")
        out["support"] = {k: _amount(v) for k, v in support.items()}
    return out


def money(amount: float, currency: str) -> str:
    return "%s %s" % (currency, "{:,.2f}".format(amount))


__all__ = ["parse_price_table", "money", "CURRENCIES", "SUPPORT"]
