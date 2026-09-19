#!/usr/bin/env python3
"""Ask another agent something, and leave a record that outlives the session.

WHY THIS EXISTS
---------------
Asked for on 2026-09-19, verbatim: "a FAQ page should be built and logged where
you ask questions like these to Gemini or grok or whoever asks whoever, gets
logged so that there is full traceability - doesn't have to be showcased but it
should live somewhere in GIT and auditable."

He is right that it was missing. Four consultations with grok and one with
gemini happened tonight and shaped what got built - the ruling that the six
board-family pages must keep their own palettes, the decision to keep merges off
the console, the sender allowlist on the WhatsApp gate. Every one of those
changed the work, and none of them existed anywhere a person could audit. The
board carries the RESULT of a decision; it does not carry the argument.

TRACEABILITY BY CONSTRUCTION, NOT BY DISCIPLINE
-----------------------------------------------
The record is written by the same call that asks the question. There is no
"remember to log it" step, because a logging step that depends on remembering is
a logging step that is missing exactly when the exchange mattered.

IT IS A TRAINING CORPUS, NOT AN ARCHIVE
---------------------------------------
Second instruction, same night: "make it so that it works as a learning and
training model; how to ask questions who answers; what was the right answer;
who answered it wrong and how to learn from it."

A VERDICT REQUIRES A RUN. Third instruction, same night, and it is the one that
makes the rest of this honest: "the right or wrong answer is only determined
after the implementation runs; and its a good way to get smarter by making
mistakes."

So a record moves through states, and the tool REFUSES to score one that has not
met reality yet - no evidence, no verdict:

    unverified  asked, nothing built from it yet
    built       implemented, not yet proven by a run
    correct     it ran, and the advice held
    partly      it ran, and some of the advice held
    wrong       it ran, and the advice did not hold
    unused      a decision went another way; say why

Every verdict carries `evidence` - a commit, a deployment version, a
measurement, a log line. An opinion about an opinion is not a verdict.

    happened   what actually occurred when it ran, measured
    lesson     what to do differently next time
    ask_lesson what was wrong with the QUESTION, when that is the fault

A `wrong` here is the most valuable record in the directory. Nobody gets
smarter from a file full of `correct`.

AND THE RECORD KEEPS MY OWN POSITION, WHICH IS THE POINT
--------------------------------------------------------
Fourth instruction: "or else if you ask me and i just say YAH or go with your
recommendation, we have to live with existing bias and issues in your head."

Exactly right, and it is the flaw in asking HIM to arbitrate engineering. A
rubber stamp launders my opinion into a decision: the same bias comes out the
other side wearing an approval. So:

- every record states MY POSITION going in, before the answer
- it marks whether the peer DISSENTED from it
- the scoreboard counts the only number that proves consulting is worth
  anything: dissents that reality later proved right

A consultation where the peer agreed with me and reality agreed with both of us
teaches nothing and is filed as such. His approval is not evidence, and it never
scores a record - only a run does.

An archive of advice nobody scored teaches nothing. A record that says "grok was
right about the palettes and wrong about what was already shipped, and the
second one was my fault for not telling it" is the thing that makes the next
routing decision evidence rather than habit.

The index carries a scoreboard built from those verdicts - who has been right,
about what - and a lessons section built from every record that went wrong.

WHAT GETS WRITTEN
-----------------
docs/consultations/YYYY-MM-DD-<slug>.md - one file per exchange, carrying who
asked, who answered, which model, when, the question verbatim, the answer
verbatim, and the verdict. The index at docs/consultations/README.md is
regenerated from the files, so it cannot drift from them.

WHAT DOES NOT GET WRITTEN
-------------------------
Credentials, obviously - the transports read .env themselves and nothing from it
reaches a prompt. And nothing is redacted on the way OUT either: if a question
was asked, the file says what was asked. A sanitised record of a decision is a
record of a different decision.

USAGE
    python scripts/consult.py ask --to gemini --file question.txt --subject "..."
    python scripts/consult.py ask --to grok --thread site --file q.txt --subject "..."
    python scripts/consult.py log --to grok --thread site --subject "..."   # backfill
    python scripts/consult.py index
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
OUT = REPO / "docs" / "consultations"
ME = "claude-code-cli"


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "consultation")[:60]


def now() -> datetime:
    return datetime.now(timezone.utc)


def ask_grok(question: str, thread: str) -> tuple:
    """Through grok_thread.py so the standing conversation keeps its context."""
    tmp = REPO / ".consult_q.txt"
    tmp.write_text(question, encoding="utf-8", newline="\n")
    try:
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "grok_thread.py"), "say",
             "-t", thread, "--file", str(tmp), "--max-tokens", "1400"],
            capture_output=True, text=True, timeout=600, cwd=str(REPO))
        body = (p.stdout or "").strip()
        # grok_thread prints a trailing accounting line; keep it as metadata
        # rather than pretending the answer included it.
        meta = ""
        if "\n--- thread" in body:
            body, _, meta = body.rpartition("\n--- thread")
            meta = "thread" + meta
        if not body:
            body = (p.stderr or "").strip() or "(no output)"
        return body.strip(), (meta.strip() or "grok-4.6 via scripts/grok_thread.py")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def ask_gemini(question: str) -> tuple:
    p = subprocess.run(
        [sys.executable, str(SCRIPTS / "gemini_agent.py"), "say", question],
        capture_output=True, text=True, timeout=600, cwd=str(REPO))
    body = (p.stdout or "").strip() or (p.stderr or "").strip() or "(no output)"
    return body, "gemini via scripts/gemini_agent.py"


ASKERS = {"grok": ask_grok, "gemini": ask_gemini}


def write_record(to: str, subject: str, question: str, answer: str, how: str,
                 my_position: str = "", dissent: str = "unscored") -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = now()
    path = OUT / ("%s-%s.md" % (stamp.strftime("%Y-%m-%d"), slugify(subject)))
    n = 2
    while path.exists():
        path = OUT / ("%s-%s-%d.md" % (stamp.strftime("%Y-%m-%d"), slugify(subject), n))
        n += 1
    path.write_text(
        "---\n"
        "asked_by: %s\n"
        "answered_by: %s\n"
        "at: %s\n"
        "transport: %s\n"
        "subject: %s\n"
        "outcome: unverified\n"
        "dissent: %s\n"
        # PUBLIC IS OPT-IN, ALWAYS. Asked for the same night: some form of this
        # belongs on sfdc24.com/method as an exhibit. These records carry
        # deployment ids, file paths, a phone number and arguments about
        # credentials, so nothing goes out until a human has read that specific
        # file and said so. `consult.py publish <file>` is that act.
        "public: no\n"
        "---\n\n"
        "# %s\n\n"
        "## My position going in\n\n%s\n\n"
        "## Asked\n\n%s\n\n## Answered\n\n%s\n\n"
        "## Verdict\n\n"
        "_Unverified. Fill this in once the advice has met reality:_\n\n"
        "```\n"
        "python scripts/consult.py verdict %s --outcome correct|partly|wrong|unused \\\n"
        "    --happened \"what actually occurred, measured\" \\\n"
        "    --lesson \"what to do differently\" [--ask-lesson \"what the QUESTION got wrong\"]\n"
        "```\n"
        % (ME, to, stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), how, subject, dissent,
           subject,
           (my_position.strip() or "_not recorded - the record is weaker for it_"),
           question.strip(), answer.strip(), path.name),
        encoding="utf-8", newline="\n")
    return path


def set_verdict(path: Path, outcome: str, happened: str, lesson: str,
                evidence: str, dissent: str = "", ask_lesson: str = "") -> Path:
    """Score an answer after it has met reality.

    EVIDENCE IS MANDATORY for a scored outcome, and the caller enforces it. "The
    right or wrong answer is only determined after the implementation runs" -
    so a verdict that cannot name a commit, a deployment version, a measurement
    or a log line is not a verdict, it is a second opinion about an opinion.

    The verdict REPLACES the placeholder rather than appending, so a record
    cannot end up carrying both "unverified" and a verdict - a file that says
    two things is a file nobody trusts.
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^outcome: .*$", "outcome: " + outcome, text, count=1, flags=re.M)
    if dissent:
        if re.search(r"^dissent: ", text, flags=re.M):
            text = re.sub(r"^dissent: .*$", "dissent: " + dissent, text, count=1, flags=re.M)
        else:
            text = text.replace("outcome: ", "dissent: %s\noutcome: " % dissent, 1)
    head, sep, _tail = text.partition("## Verdict")
    if not sep:
        head, sep = text, "## Verdict"
    block = [
        "## Verdict", "",
        "**%s**, scored %s." % (outcome.upper(), now().strftime("%Y-%m-%dT%H:%M:%SZ")), "",
        "**Evidence it ran:** %s" % evidence.strip(), "",
        "**What actually happened:** %s" % happened.strip(), "",
        "**Lesson:** %s" % lesson.strip(), "",
    ]
    if ask_lesson.strip():
        block += ["**What the question got wrong:** %s" % ask_lesson.strip(), ""]
    path.write_text(head.rstrip() + "\n\n" + "\n".join(block), encoding="utf-8", newline="\n")
    return path


def read_records() -> list:
    """Every record with its front matter and its lesson lines, if any."""
    out = []
    for f in sorted(OUT.glob("*.md")):
        if f.name.lower() == "readme.md":
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        parts = text.split("---")
        meta = {}
        if len(parts) > 1:
            for line in parts[1].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
        lesson = ""
        m = re.search(r"\*\*Lesson:\*\*\s*(.+)", text)
        if m:
            lesson = m.group(1).strip()
        ask_lesson = ""
        m = re.search(r"\*\*What the question got wrong:\*\*\s*(.+)", text)
        if m:
            ask_lesson = m.group(1).strip()
        out.append({"file": f, "meta": meta, "lesson": lesson, "ask_lesson": ask_lesson,
                    "text": text})
    return out


def scoreboard(records: list) -> list:
    """Who has been right, about what, and whether disagreeing with me helped.

    The last column is the only one that proves consulting is worth anything: a
    peer that agrees with me and is right teaches nothing, because I was already
    going to do that. A peer that DISAGREED and turned out right is the whole
    return on asking.
    """
    by = {}
    for r in records:
        who = r["meta"].get("answered_by", "?")
        s = by.setdefault(who, {"n": 0, "correct": 0, "partly": 0, "wrong": 0,
                                "open": 0, "dissent": 0, "dissent_right": 0})
        s["n"] += 1
        outcome = r["meta"].get("outcome", "unverified")
        dissent = r["meta"].get("dissent", "unscored") == "yes"
        if dissent:
            s["dissent"] += 1
        if outcome in ("correct", "partly", "wrong"):
            s[outcome] += 1
            if dissent and outcome in ("correct", "partly"):
                s["dissent_right"] += 1
        else:
            s["open"] += 1
    return sorted(by.items())


def rebuild_index() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    records = read_records()
    rows = [(r["meta"].get("at", ""), r["meta"].get("answered_by", "?"),
             r["meta"].get("subject", r["file"].stem), r["file"].name,
             r["meta"].get("outcome", "unverified"),
             r["meta"].get("dissent", "unscored")) for r in records]
    rows.sort(reverse=True)
    body = [
        "# Who asked whom, and what they said back",
        "",
        "Every cross-agent consultation on this fleet, written by the call that",
        "made it. Asked for on 2026-09-19: full traceability, in git, auditable,",
        "and not required to be public.",
        "",
        "The board carries the RESULT of a decision. This carries the argument -",
        "which is the part that explains why the result looks the way it does.",
        "",
        "Written by `scripts/consult.py`, which asks and records in one call, so",
        "there is no step anyone can forget. The index is regenerated from the",
        "files and cannot drift from them.",
        "",
        "## Scoreboard",
        "",
        "A verdict requires a RUN. Nothing here is scored by argument, and an",
        "approval from Mr Salam never scores a record - if he says \"go with your",
        "recommendation\", the same bias comes out the other side wearing an",
        "approval. Only what happened when it ran counts.",
        "",
        "`dissent right` is the column that matters: a peer agreeing with me and",
        "being right teaches nothing, because that was already going to happen.",
        "A peer that DISAGREED and turned out right is the entire return on asking.",
        "",
        "| answered by | asked | correct | partly | wrong | open | dissented | dissent right |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for who, s in scoreboard(records):
        body.append("| %s | %d | %d | %d | %d | %d | %d | **%d** |" % (
            who, s["n"], s["correct"], s["partly"], s["wrong"], s["open"],
            s["dissent"], s["dissent_right"]))

    body += ["", "## Lessons, which is what the wrong ones are for", ""]
    lessons = [r for r in records if r["lesson"]]
    if not lessons:
        body.append("_None scored yet._")
    for r in lessons:
        body.append("- **%s** (%s, %s) — %s" % (
            r["meta"].get("subject", r["file"].stem),
            r["meta"].get("answered_by", "?"),
            r["meta"].get("outcome", "?"),
            r["lesson"]))
        if r["ask_lesson"]:
            body.append("  - _on the asking:_ %s" % r["ask_lesson"])

    body += ["", "## Every consultation", "",
             "| when (UTC) | answered by | outcome | dissent | subject |",
             "|---|---|---|---|---|"]
    for at, who, subject, name, outcome, dissent in rows:
        body.append("| %s | %s | %s | %s | [%s](%s) |" % (
            at or "?", who, outcome, dissent, subject, name))
    body.append("")
    body.append("%d consultation%s recorded." % (len(rows), "" if len(rows) == 1 else "s"))
    body.append("")
    path = OUT / "README.md"
    path.write_text("\n".join(body), encoding="utf-8", newline="\n")
    return path


def cmd_ask(args) -> int:
    question = Path(args.file).read_text(encoding="utf-8") if args.file else args.question
    if not question or not question.strip():
        raise SystemExit("nothing to ask")
    fn = ASKERS[args.to]
    answer, how = fn(question, args.thread) if args.to == "grok" else fn(question)
    path = write_record(args.to, args.subject, question, answer, how,
                        my_position=args.my_position)
    rebuild_index()
    print(answer)
    print("\n--- recorded: %s ---" % path.relative_to(REPO))
    return 0


def cmd_log(args) -> int:
    """Backfill from a grok_thread transcript that already happened."""
    src = REPO / ".grok_threads" / ("%s.json" % args.thread)
    if not src.exists():
        raise SystemExit("no transcript at %s" % src)
    turns = json.loads(src.read_text(encoding="utf-8"))
    pairs = [(turns[i]["content"], turns[i + 1]["content"])
             for i in range(0, len(turns) - 1, 2)
             if turns[i]["role"] == "user" and turns[i + 1]["role"] == "assistant"]
    if not pairs:
        raise SystemExit("no complete exchanges in that transcript")
    made = []
    for n, (q, a) in enumerate(pairs, 1):
        subject = "%s (%d of %d)" % (args.subject, n, len(pairs))
        made.append(write_record(args.to, subject, q, a,
                                 "grok-4.6 via scripts/grok_thread.py, backfilled"))
    rebuild_index()
    for m in made:
        print("recorded %s" % m.relative_to(REPO))
    return 0


def cmd_verdict(args) -> int:
    path = OUT / args.record if not Path(args.record).exists() else Path(args.record)
    if not path.exists():
        raise SystemExit("no record at %s" % path)
    if args.outcome in ("correct", "partly", "wrong") and not (args.evidence or "").strip():
        # THE WHOLE POINT, ENFORCED RATHER THAN ASKED FOR. "The right or wrong
        # answer is only determined after the implementation runs."
        raise SystemExit(
            "refusing to score %s without --evidence. A verdict names the run: a "
            "commit, a deployment version, a measurement, or a log line. Without "
            "one this is an opinion about an opinion." % args.outcome)
    set_verdict(path, args.outcome, args.happened, args.lesson,
                args.evidence or "", args.dissent or "", args.ask_lesson or "")
    rebuild_index()
    print("scored %s as %s" % (path.name, args.outcome))
    return 0


def cmd_publish(args) -> int:
    """Mark one record publishable. A human act, one file at a time.

    These carry deployment ids, file paths, a phone number and arguments about
    credentials. Nothing reaches sfdc24.com until somebody has read that
    specific file and said so.
    """
    path = OUT / args.record if not Path(args.record).exists() else Path(args.record)
    if not path.exists():
        raise SystemExit("no record at %s" % path)
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^public: .*$", "public: yes", text, count=1, flags=re.M)
    path.write_text(text, encoding="utf-8", newline="\n")
    rebuild_index()
    print("%s is now publishable" % path.name)
    return 0


def cmd_exhibit(args) -> int:
    """The data behind the /method/ exhibit: public records only.

    It emits DATA, not markup. The page that renders it lives in the site repo
    and is subject to its own guards - the claim ledger, the first-person ban -
    and a generator that wrote HTML here would route around both.
    """
    records = [r for r in read_records() if r["meta"].get("public") == "yes"]
    out = {
        "generated": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {who: s for who, s in scoreboard(read_records())},
        "consultations": [{
            "at": r["meta"].get("at"),
            "answered_by": r["meta"].get("answered_by"),
            "subject": r["meta"].get("subject"),
            "outcome": r["meta"].get("outcome"),
            "dissent": r["meta"].get("dissent"),
            "lesson": r["lesson"],
        } for r in records],
    }
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("wrote %s - %d public of %d recorded" % (dest, len(records), len(read_records())))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Ask another agent, and record it.")
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("ask", help="ask and record in one call")
    a.add_argument("--to", required=True, choices=sorted(ASKERS))
    a.add_argument("--subject", required=True)
    a.add_argument("--file")
    a.add_argument("--question")
    a.add_argument("--my-position", default="",
                   help="what you would have done without asking. Recording it is "
                        "what makes a later dissent measurable.")
    a.add_argument("--thread", default="site", help="grok only: which standing thread")

    l = sub.add_parser("log", help="backfill records from an existing transcript")
    l.add_argument("--to", default="grok")
    l.add_argument("--thread", default="site")
    l.add_argument("--subject", required=True)

    v = sub.add_parser("verdict", help="score a record, after it ran")
    v.add_argument("record")
    v.add_argument("--outcome", required=True,
                   choices=["built", "correct", "partly", "wrong", "unused", "unverified"])
    v.add_argument("--happened", default="")
    v.add_argument("--lesson", default="")
    v.add_argument("--evidence", default="")
    v.add_argument("--dissent", default="", choices=["", "yes", "no"])
    v.add_argument("--ask-lesson", dest="ask_lesson", default="")

    p = sub.add_parser("publish", help="mark one record publishable")
    p.add_argument("record")

    e = sub.add_parser("exhibit", help="emit the public data for the site exhibit")
    e.add_argument("--out", default=str(REPO / "docs" / "consultations" / "exhibit.json"))

    sub.add_parser("index", help="regenerate the index from the files")

    args = ap.parse_args()
    if args.cmd == "ask":
        return cmd_ask(args)
    if args.cmd == "log":
        return cmd_log(args)
    if args.cmd == "verdict":
        return cmd_verdict(args)
    if args.cmd == "publish":
        return cmd_publish(args)
    if args.cmd == "exhibit":
        return cmd_exhibit(args)
    if args.cmd == "index":
        print("wrote %s" % rebuild_index().relative_to(REPO))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
