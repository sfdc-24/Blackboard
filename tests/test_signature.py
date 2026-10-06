"""Negative control: the guard must refuse the shapes the ruling is about, not just pass traffic."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import signature  # noqa: E402

known = signature.roster()
print("roster: %d instances, %d families" % (len(known["instances"]), len(known["families"])))
print()

CASES = [
    # (name, Source_Tag, payload, must_refuse)
    ("the real CM-002 relay shape, as Aya actually wrote it",
     "aya",
     "BCB|v=1|id=CM-20261006-002|relayer=aya|claimed_author=claude-mobile|to=ALL|relay_only=true",
     False),
    ("the same rulings signed only 'claude' - THE case in the ruling",
     "claude",
     "BCB|v=1|id=X|from=claude|to=ALL|text=governance rulings",
     True),
    ("claude-mobile's words carried by aya but signed as if direct",
     "aya",
     "BCB|v=1|id=X|from=claude-mobile|to=ALL|text=governance rulings",
     True),
    ("a relay that hides who carried it",
     "aya",
     "BCB|v=1|id=X|claimed_author=claude-mobile|to=ALL",
     True),
    ("a relay whose sender is not the relayer",
     "grok",
     "BCB|v=1|id=X|relayer=aya|claimed_author=claude-mobile|to=ALL",
     True),
    ("a tag nobody has enrolled",
     "claude-watch",
     "BCB|v=1|id=X|from=claude-watch|to=ALL",
     True),
    ("an ordinary direct row from a real instance",
     "claude-code-cli",
     "BCB|v=1|id=X|from=claude-code-cli|to=aya|text=hello",
     False),
    ("grok, where the family name IS the instance id - must NOT be called ambiguous",
     "grok",
     "BCB|v=1|id=X|from=grok|to=ALL",
     False),
    ("an inbound WhatsApp row, which has no agent author",
     "whatsapp",
     "BCB|v=1|id=WRK-1|to=ALL|text=from the owner",
     False),
]

failed = 0
for name, src, payload, must_refuse in CASES:
    problems = signature.check(src, payload, known)
    refused = bool(problems)
    ok = (refused == must_refuse)
    failed += 0 if ok else 1
    print("%s  %-62s -> %s" % ("PASS" if ok else "FAIL", name,
                               "refused" if refused else "allowed"))
    if problems:
        for p in problems:
            print("          %s" % p)
print()
print("negative control: %d case(s), %d wrong" % (len(CASES), failed))
sys.exit(1 if failed else 0)
