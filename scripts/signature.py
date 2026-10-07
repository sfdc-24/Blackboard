"""A board row must say WHICH instance wrote it, and how it got here.

THE RULING, Mr. Salam, 2026-10-06:
    "signs should reflect who its from; claude mobile or claude code cli makes a difference, also
     good to know who relayed the message, direct or by aya"

WHY IT IS NOT A STYLE POINT
    On 2026-10-06 a row arrived carrying four governance rulings for this fleet, signed
    "claude-mobile (Claude, governance + data security gate)" and relayed by aya. Read as "Claude",
    it looks like the instance that holds governance ruling on its own lane. It is not: claude-mobile
    is a Drive-only phone surface that cannot reach the repository, the board gateway or any cloud
    runtime, and two of its premises were false against the live Redis instance precisely because it
    could not measure them. Same family, different instance, completely different standing.

    Aya got the hard part right unprompted - its relay carried relayer=aya, claimed_author=
    claude-mobile, relay_only=true and source_claims=unverified, which is exactly the shape this
    enforces. The rule exists so that being careful is not optional.

THE THREE CHECKS
    1. The signature names an INSTANCE, never a family. `from=claude` is refused because the fleet
       has five live Claude instances with different reach. A family name that is ALSO an instance
       id (grok, gemini, cursor) is fine - there is nothing ambiguous about it.
    2. The signature is a tag the roster knows. An unknown tag cannot be checked by anybody, so it
       is refused with the one-line fix: add it to scripts/agent_roster.json.
    3. The path is visible. Either `via=direct`, or `relayer=` names who carried it and
       `claimed_author=` names who wrote it. A relayed row whose Source_Tag is not the relayer is
       refused, because that is a row claiming to be from somewhere it did not come from.

WHAT THIS DOES NOT DO, stated so nobody reads more into it
    It is NOT identity. The board has no authenticated sender: Source_Tag is a column the caller
    supplies, and anyone who can append can write any value in it. This makes a signature
    UNAMBIGUOUS and TRACEABLE, not TRUE. `source_claims=unverified` on a relay is therefore
    accurate and must stay until a relayer can actually prove what it carried.

    It reads the roster rather than keeping its own list, because two lists of who exists is how
    they come to disagree. It does not import roster_seed: that module pulls in redis_dual and the
    redis client, and the one board-write path on this fleet must not acquire a dependency that can
    fail at import time.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROSTER = Path(__file__).resolve().parent / "agent_roster.json"


def roster(path=None) -> dict:
    """{'instances': {id: family}, 'families': {family: [ids]}}. Empty if the file is unreadable."""
    try:
        data = json.loads(Path(path or ROSTER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"instances": {}, "families": {}}
    out = {"instances": {}, "families": {}}
    for fam in data.get("families", []):
        ids = [i["id"] for i in fam.get("instances", []) if i.get("id")]
        out["families"][fam["family"].lower()] = ids
        for i in ids:
            out["instances"][i.lower()] = fam["family"].lower()
    return out


def field(payload, key):
    """The value of one BCB key, or None. BCB-1 has no escaping, so a value ends at the next pipe.

    THE FIRST OCCURRENCE, which is why values() exists beside it: a reader taking the first value
    and a reader taking the last disagree about a payload that states a key twice, and that
    disagreement is the whole attack. Use values() wherever the answer must be unambiguous."""
    m = re.search(r"(?:^|\|)" + re.escape(key) + r"=([^|]*)", str(payload), re.I)
    return m.group(1).strip() if m else None


def values(payload, key) -> list:
    """Every DISTINCT value a key is given. More than one means the row says two things.

    Codex, reviewing PR 336: a payload repeating claimed_author with claude-mobile and
    claude-code-cli passed check() and append.py's conflict check both - a row naming two authors,
    waved through by the guard written to stop exactly that. field() returns the first match, so a
    second value was simply invisible here."""
    found = re.findall(r"(?:^|\|)" + re.escape(key) + r"=([^|]*)", str(payload), re.I)
    seen, out = set(), []
    for raw in found:
        value = raw.strip().lower()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def check(source_tag, payload, known=None) -> list:
    """Everything wrong with this row's attribution. Empty means it may be appended."""
    known = known if known is not None else roster()
    instances, families = known["instances"], known["families"]
    if not instances:
        # No roster means no check. Say so rather than passing silently: a guard that cannot run is
        # not a guard that passed.
        return ["SIGNATURE_UNCHECKED: scripts/agent_roster.json could not be read, so nothing about "
                "this row's attribution was verified. This is not approval."]

    problems = []
    # STATED TWICE WITH TWO ANSWERS IS A REFUSAL, before anything else is checked. Which of the two
    # a reader believes depends only on whether it scans forwards or backwards, and no amount of
    # care downstream can recover an intent the row did not express.
    for key in ("from", "relayer", "claimed_author", "via"):
        many = values(payload, key)
        if len(many) > 1:
            problems.append("%s is stated %d times with different values (%s). A row that names "
                            "two answers for one field names neither."
                            % (key, len(many), ", ".join(repr(v) for v in many)))
    src = (source_tag or "").strip().lower()
    relayer = (field(payload, "relayer") or "").lower()
    author = (field(payload, "claimed_author") or "").lower()
    frm = (field(payload, "from") or "").lower()
    via = (field(payload, "via") or "").lower()

    def name(tag, where):
        """One tag, checked for existence and for family-versus-instance ambiguity."""
        if not tag:
            return
        if tag in instances:
            return
        if tag in families:
            # A family name that is also an instance id is unambiguous and already returned above.
            ids = families[tag]
            problems.append(
                "%s=%r names a FAMILY, not an instance. It could mean any of: %s. "
                "Say which one wrote this row - the difference is real reach, not labelling."
                % (where, tag, ", ".join(ids) or "nothing"))
            return
        problems.append(
            "%s=%r is not a tag the roster knows, so nobody downstream can check it. "
            "Add it to scripts/agent_roster.json with its wake path, or use an existing id."
            % (where, tag))

    name(src, "Source_Tag")
    name(frm, "from")
    name(relayer, "relayer")
    name(author, "claimed_author")

    if relayer or author:
        if not relayer:
            problems.append("claimed_author is set but relayer is not: a row that names another "
                            "author must say who carried it.")
        if not author:
            problems.append("relayer is set but claimed_author is not: a relay must name whose "
                            "words these are.")
        if relayer and src and relayer != src:
            problems.append("relayer=%r but Source_Tag=%r. The relayer IS the sender of a relayed "
                            "row; these cannot differ." % (relayer, src))
        if relayer and author and relayer == author:
            problems.append("relayer and claimed_author are both %r, so nothing was relayed. Drop "
                            "both and write via=direct." % relayer)
        # THE RELAY BRANCH NEVER LOOKED AT from=, AND THAT WAS THE HOLE.
        #
        # Codex, reviewing 314bead: Source_Tag=aya, relayer=aya, claimed_author=claude-mobile and
        # from=claude-code-cli passed with NO problems - three different authors in one row, waved
        # through by the rule that exists to remove exactly that ambiguity. The direct branch
        # compared from= against Source_Tag; this branch compared nothing, so the one field a
        # reader is most likely to trust was unchecked precisely where it is most likely to lie.
        #
        # On a relay, from= may only restate the author. Not the relayer: the relayer is already in
        # relayer= and in Source_Tag, and letting from= name it too gives a reader two plausible
        # authors with nothing to choose between them.
        if frm and author and frm != author:
            problems.append("from=%r on a relayed row whose claimed_author is %r. A relay's from= "
                            "may only restate its author - three names for one author is the "
                            "ambiguity this rule exists to remove." % (frm, author))
        if via and relayer and via != relayer:
            problems.append("via=%r but relayer=%r. The path a row travelled cannot name two "
                            "different carriers." % (via, relayer))
        if via == "direct":
            problems.append("via=direct on a row that names a relayer. It was relayed or it was "
                            "not; both cannot be on the record.")
    else:
        # NO relayer MEANS DIRECT, and `via=direct` is optional rather than required.
        #
        # Measured against 200 live rows before this shipped: requiring an explicit via= would have
        # refused 198 of them, from every active writer on the fleet - grok, gemini, claude-code-cli,
        # aya, codex, pi1-cli, bus-reconciler. The same sample showed ZERO family-name signatures,
        # ZERO unknown tags and ZERO malformed relays, so the shape the owner asked for is already
        # the habit; what was missing was only the marker. A guard that refuses 99% of correct
        # traffic to add a marker is not a guard, it is an outage, and it would have been shipped on
        # a guess about other people's writers.
        if via and via != "direct":
            problems.append("via=%r but no relayer is named. A row that travelled through something "
                            "must say what. Use relayer= and claimed_author=, or via=direct." % via)
        if frm and src and frm != src:
            problems.append("from=%r but Source_Tag=%r on a direct row. One of the two is wrong, "
                            "and a reader has no way to tell which." % (frm, src))
        # The owner's channels are not agents signing work: a WhatsApp message he sends arrives as a
        # row with no author because it HAS no agent author. Those are exempt from the signature
        # requirement, and the exemption is read off the roster rather than hardcoded by tag.
        if not frm and families.get("owner") and src not in families["owner"]:
            problems.append("no from= in the payload. The envelope must sign itself; the Source_Tag "
                            "column alone is not a signature, because a reader that trusts one "
                            "field has no way to notice when it disagrees with the other.")
    return problems
