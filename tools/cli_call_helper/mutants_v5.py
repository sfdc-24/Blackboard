"""Mutants for the v5 helpers: each must make test_v5.py fail. Runs on a COPY of this folder, never in place.
Run as a script only: python mutants_v5.py"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ("call_state.py", "laptop_post.py", "call_ask.py", "call_migrate.py", "cli_call_consumer.py", "test_v5.py")
MUTANTS = [
    ("laptop_post.py", 'if not DEDUP_ENFORCED or op.get("retried"):', 'if op.get("retried"):'),
    ("laptop_post.py", '    txn.save()                                             # on disk BEFORE the append', '    pass'),
    ("laptop_post.py", '    elif seen is None:\n        return "UNKNOWN"', '    elif seen is None:\n        pass'),
    ("laptop_post.py", 'if (outcome == "ok" and value == 0) else "UNKNOWN"', 'if outcome == "ok" else "UNKNOWN"'),
    ("call_state.py", "                if clock() > end:", "                if True:\n                    break\n                if clock() > end:"),
    ("call_state.py", "    opened = [t[\"entry\"] for t in state[\"tasks\"].values() if t[\"state\"] in OPEN and t.get(\"entry\")]",
     "    opened = []"),
    ("call_ask.py", 'e.get("by") == call_bridge.BY and ', ''),
    ("call_ask.py", 'rec.get("agent") == TAG and ', ''),
    ("call_ask.py", '    if verdict != "MATCH":\n', '    if False:\n'),
    ("call_ask.py", 'ok = mine.startswith(said) if cut else mine == said[:call_bridge.ASK_CHARS]', 'ok = True'),
    ("call_ask.py", '            if open_now is None:\n                print("UNKNOWN_AUTH: no status',
     '            if False:\n                print("UNKNOWN_AUTH: no status'),
    ("call_ask.py", '            if not open_now:\n                print(closed(ask, task, txn))\n                return 2\n            if task["state"] == "ACCEPTED":',
     '            if task["state"] == "ACCEPTED":'),
    ("call_ask.py", "            if mine and canonical(mine[0]) == task[\"hash\"]:", "            if mine:"),
    ("call_ask.py", 'if item is None or card["type"] != task.get("expect"):', 'if item is None:'),
    ("call_ask.py", '    if not task.get("ops", {}).get("CLAUDE-CARD-%s" % ask):', '    if True:'),
    ("call_ask.py", '            if task["rev"] != rev:\n                print("RE-PLAN")\n                return 6\n            if open_now is None:',
     '            if open_now is None:'),
    ("call_ask.py", '    stamp = utc().strftime("%Y%m%dT%H%M%S") + os.urandom(2).hex()', '    stamp = "FIXED"'),
    ("call_migrate.py", '            st = "HISTORICAL_CLOSED"', '            st = "DONE"'),
    ("call_migrate.py", '    if pending and pending not in state["tasks"]:', '    if False:'),
    ("call_migrate.py", 'if cs.sid(old) > cs.sid(last) or cs.sid(old) > cs.sid(new):', 'if False:'),
    ("call_state.py", '        if ask in txn.state["tasks"]:\n            return False', '        if False:\n            return False'),
    ("cli_call_consumer.py", '            if t["state"] == "PENDING":\n                return ask', '            if False:\n                return ask'),
]


def main():
    killed, survived = 0, []
    for i, (name, old, new) in enumerate(MUTANTS, 1):
        tmp = tempfile.mkdtemp()
        for f in FILES:
            shutil.copy(os.path.join(HERE, f), tmp)
        path = os.path.join(tmp, name)
        src = open(path, encoding="utf-8").read()
        if src.count(old) != 1:
            print("BAD ANCHOR %d %s" % (i, name))
            survived.append(i)
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, "test_v5.py"], cwd=tmp, capture_output=True, text=True, timeout=300,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        ok = r.returncode != 0
        killed += ok
        if not ok:
            survived.append(i)
        print("%2d %s %s" % (i, "killed  " if ok else "SURVIVED", name))
        shutil.rmtree(tmp, ignore_errors=True)
    print("%d/%d killed; survived %s" % (killed, len(MUTANTS), survived or "none"))
    return 0 if not survived else 1


if __name__ == "__main__":
    sys.exit(main())
