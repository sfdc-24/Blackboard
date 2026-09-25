"""The build plan's price table - the owner's numbers, never ours.

Owner direction, 2026-09-25: the build plan PDF moves ahead with "50% to move
ahead with build and test and 50% upon complete delivery and handover with
support plan". The prices are the owner's decision (BLK-004 is open), so none
is invented: the plan says "Priced after review" unless STUDIO_PRICE_TABLE
holds a table, which is checked strictly here and at startup.

    {"currency": "CAD", "prices": {"website": {"label": "Company website build", "amount": 4800}}}

currency is CAD or USD; each key is a homepage topic; label is one plain line
of at most 80 characters; amount is a number from 1 to 1,000,000 with at most
two decimals. Anything else refuses the whole table.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata

CURRENCIES = ("CAD", "USD")
LABEL_MAX = 80
AMOUNT_MAX = 1_000_000
_BAD = re.compile(r"[<>\x00-\x1f\x7f]")
_UNSAFE = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn")


def parse_price_table(raw: str, topics) -> dict | None:
    """None for an empty setting; the checked table; ValueError for anything else."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        table = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("STUDIO_PRICE_TABLE is not JSON") from exc
    if not isinstance(table, dict) or set(table) != {"currency", "prices"}:
        raise ValueError("STUDIO_PRICE_TABLE must be exactly {currency, prices}")
    if table["currency"] not in CURRENCIES:
        raise ValueError("STUDIO_PRICE_TABLE currency must be one of %s" % ", ".join(CURRENCIES))
    prices = table["prices"]
    if not isinstance(prices, dict) or not prices:
        raise ValueError("STUDIO_PRICE_TABLE prices must be a non-empty object")
    out = {}
    for topic, item in prices.items():
        if topic not in topics:
            raise ValueError("STUDIO_PRICE_TABLE names an unknown topic")
        if not isinstance(item, dict) or set(item) != {"label", "amount"}:
            raise ValueError("each STUDIO_PRICE_TABLE price must be exactly {label, amount}")
        label, amount = item["label"], item["amount"]
        if (not isinstance(label, str) or not label.strip() or len(label.strip()) > LABEL_MAX
                or _BAD.search(label) or any(unicodedata.category(c) in _UNSAFE for c in label)):
            raise ValueError("a STUDIO_PRICE_TABLE label must be one plain line of at most %d characters" % LABEL_MAX)
        if (type(amount) not in (int, float) or not math.isfinite(amount) or not 1 <= amount <= AMOUNT_MAX
                or round(amount, 2) != amount):
            raise ValueError("a STUDIO_PRICE_TABLE amount must be 1 to %d with at most two decimals" % AMOUNT_MAX)
        out[topic] = {"label": label.strip(), "amount": float(amount)}
    return {"currency": table["currency"], "prices": out}


def money(amount: float, currency: str) -> str:
    return "%s %s" % (currency, "{:,.2f}".format(amount))


__all__ = ["parse_price_table", "money", "CURRENCIES"]
