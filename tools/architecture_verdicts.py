"""Read exact-head verdicts from the Blackboard board up to an evidence cutoff
and write them into the architecture ADR's review records.

The architecture PDF states one evidence cutoff. Verdicts are never typed by
hand: this reads every board row at or before the cutoff whose id starts with
CODEX- or CURSOR, or whose text contains verdict=, matches it to each review
record by exact_head / reviewed_head, and keeps the latest GO or NO-GO per
head. A head with none is PENDING. It also lists verdict rows for other heads
of the same pull requests, so a newer head is not missed.

    python tools/architecture_verdicts.py --cutoff 2026-09-26T03:55:00Z \
        --env ../Blackboard/.env [--write]

It prints a table; --write updates the verdict and row fields in the ADR.
The PDF build itself never reads the board, so CI stays reproducible.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def bus(env: dict, payload: dict, hops: int = 8):
    """POST to the Apps Script gateway, following its redirect to the result by GET."""
    opener = urllib.request.build_opener(_NoRedirect)
    url = env["BUS_URL"]
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    for _ in range(hops):
        try:
            with opener.open(req, timeout=180) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urljoin(url, loc) if loc.startswith("/") else loc
                req = urllib.request.Request(url, method="GET")
                continue
            return e.code, body
    return 0, "too many redirects"


def parse_ts(value) -> datetime | None:
    """An aware datetime, or None when the cell is not an ISO instant."""
    text = str(value or "").strip()
    if not text or len(text) < 19 or text[4] != "-":
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

ADR = ROOT / "docs" / "ADR-20260925-BLACKBOARD-MINIBUS-MULTIAGENT-CONTROL-PLANE.md"
CLAIMS_START = "<!-- architecture-pdf-claims:start -->"
CLAIMS_END = "<!-- architecture-pdf-claims:end -->"
FIELD = " || "
SOURCE = " | Source: "
REVIEW_LINE = re.compile(r"^- `(r\.[^`]+)` (.*)$")


def row_fields(payload: str) -> dict[str, str]:
    """BCB payload 'k=v|k=v|...' into a dict (first occurrence wins)."""
    out: dict[str, str] = {}
    for part in str(payload).split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            out.setdefault(k.strip(), v.strip())
    return out


def verdict_of(value: str) -> str | None:
    v = value.upper().replace("_", "-").strip()
    if "NO-GO" in v:
        return "NO-GO"
    if v == "GO":
        return "GO"
    return None


def pr_number(text: str) -> str:
    found = re.findall(r"(\d+)\s*$", str(text).strip().rstrip("/"))
    return found[-1] if found else ""


def load_reviews(adr_text: str) -> list[dict]:
    block = adr_text.split(CLAIMS_START, 1)[1].split(CLAIMS_END, 1)[0]
    reviews = []
    for line in block.strip().splitlines():
        m = REVIEW_LINE.match(line)
        if not m:
            continue
        body, source = m.group(2).split(SOURCE, 1)
        ref, subject, head, verdict, row, note = [f.strip() for f in body.split(FIELD)]
        repo = "sfdc24-site" if "Site #" in ref or "Site #" in subject else "Blackboard"
        reviews.append({"key": m.group(1), "ref": ref, "subject": subject, "head": head,
                        "verdict": verdict, "row": row, "note": note, "source": source,
                        # the PR number follows '#' in the reference, else in the subject
                        # (r5d is the release path for Blackboard #272, and its subject says so)
                        "repo": repo, "pr": (re.search(r"#(\d+)", ref) or re.search(r"#(\d+)", subject)).group(1),
                        "line": line})
    return reviews


def read_board(env_path: Path) -> list[list]:
    env = {}
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    code, body = bus(env, {"secret": env["BUS_SECRET"], "action": "read",
                                 "title": "Blackboard - Alpha DB"})
    data = json.loads(body)
    if "rows" not in data:
        raise SystemExit("board read degraded (HTTP %s); retry, do not treat as empty" % code)
    return [r for r in data["rows"] if isinstance(r, list)]


def verdict_rows(rows: list[list], cutoff) -> list[dict]:
    out = []
    for r in rows:
        ts = parse_ts(r[1] if len(r) > 1 else None)
        if ts is None or ts > cutoff:
            continue
        payload = str(r[5]) if len(r) > 5 else ""
        f = row_fields(payload)
        rid = f.get("id") or str(r[0])
        if not (rid.startswith(("CODEX-", "CURSOR")) or "verdict=" in payload):
            continue
        verdict = verdict_of(f.get("verdict", ""))
        heads = [h for h in (f.get("exact_head", ""), f.get("reviewed_head", "")) if h]
        if verdict is None or not heads:
            continue
        repo_hint = (f.get("repo", "") + " " + f.get("pr", "") + " " + f.get("project", "")).lower()
        out.append({"id": rid, "ts": ts, "verdict": verdict, "heads": [h[:7] for h in heads],
                    "pr": pr_number(f.get("pr", "")), "site": "sfdc24-site" in repo_hint or "sfdc24_site" in repo_hint})
    return out


def resolve(reviews: list[dict], vrows: list[dict]) -> tuple[list[dict], list[dict]]:
    table = []
    for rec in reviews:
        is_r5d = rec["key"] == "r.r5d"
        cands = [v for v in vrows if rec["head"][:7] in v["heads"]
                 and (("R5D" in v["id"]) == is_r5d)]
        cands.sort(key=lambda v: v["ts"])
        if cands:
            last = cands[-1]
            table.append({**rec, "new_verdict": last["verdict"], "new_row": last["id"],
                          "at": last["ts"].strftime("%Y-%m-%dT%H:%M:%SZ")})
        else:
            table.append({**rec, "new_verdict": "PENDING", "new_row": rec["row"], "at": "-"})
    listed = {(r["repo"], r["pr"], r["head"][:7]) for r in reviews}
    newest: dict[tuple[str, str], str] = {}
    for t in table:
        key = (t["repo"], t["pr"])
        newest[key] = max(newest.get(key, ""), t["at"] if t["at"] != "-" else "")
    unlisted = []
    for v in vrows:
        repo = "sfdc24-site" if v["site"] else "Blackboard"
        at = v["ts"].strftime("%Y-%m-%dT%H:%M:%SZ")
        for h in v["heads"]:
            # a head of a listed PR, reviewed after the newest listed verdict: likely newer
            if (repo, v["pr"]) in newest and (repo, v["pr"], h) not in listed and at > newest[(repo, v["pr"])]:
                unlisted.append({"repo": repo, "pr": v["pr"], "head": h, "verdict": v["verdict"],
                                 "id": v["id"], "at": at})
    return table, unlisted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", required=True)
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    cutoff = parse_ts(args.cutoff)
    if cutoff is None:
        raise SystemExit("bad --cutoff")
    cutoff = cutoff.astimezone(timezone.utc)
    adr_text = io.open(ADR, encoding="utf-8", newline="").read()
    reviews = load_reviews(adr_text)
    vrows = verdict_rows(read_board(Path(args.env)), cutoff)
    table, unlisted = resolve(reviews, vrows)

    print("Evidence cutoff: %s" % cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"))
    print("| record | PR | head | verdict | verdict row | row time |")
    print("|---|---|---|---|---|---|")
    for t in table:
        print("| %s | %s #%s | %s | %s | %s | %s |" % (t["key"], t["repo"], t["pr"], t["head"],
                                                       t["new_verdict"], t["new_row"], t["at"]))
    if unlisted:
        print("\nVerdict rows for heads not in the ADR, newer than its latest listed verdict:")
        for u in sorted(unlisted, key=lambda u: u["at"]):
            print("  %s #%s %s: %s  %s  %s" % (u["repo"], u["pr"], u["head"], u["verdict"], u["id"], u["at"]))
    if args.write:
        for t in table:
            ref, subject, head, _, _, note = [f.strip() for f in t["line"].split("` ", 1)[1].split(SOURCE, 1)[0].split(FIELD)]
            new = "- `%s` %s || %s || %s || %s || %s || %s%s%s" % (
                t["key"], ref, subject, head, t["new_verdict"], t["new_row"], note, SOURCE, t["source"])
            adr_text = adr_text.replace(t["line"], new, 1)
        # The one evidence cutoff: the addendum's statement and the PDF contract.
        stamp = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        adr_text, n = re.subn(r"^Evidence cutoff: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\.$",
                              "Evidence cutoff: %s." % stamp, adr_text, count=1, flags=re.M)
        if n != 1:
            raise SystemExit("the addendum has no 'Evidence cutoff: <instant>.' line")
        start = adr_text.index("<!-- architecture-pdf-contract:start -->")
        end = adr_text.index("<!-- architecture-pdf-contract:end -->")
        contract = json.loads(adr_text[start:end].split("```json", 1)[1].rsplit("```", 1)[0])
        contract["facts_refreshed_iso"] = stamp
        contract["facts_refreshed_label"] = "evidence cutoff " + stamp
        adr_text = (adr_text[:start] + "<!-- architecture-pdf-contract:start -->\n```json\n"
                    + json.dumps(contract, indent=2, ensure_ascii=False) + "\n```\n" + adr_text[end:])
        io.open(ADR, "w", encoding="utf-8", newline="").write(adr_text)
        print("\nADR review records and evidence cutoff updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
