"""Mutants for the v5.1 helpers and the 6a/6b bridge source: each must make its tests fail. Runs on COPIES, never in
place. Run as a script only: python mutants_v5.py"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
FILES = ("call_state.py", "laptop_post.py", "call_ask.py", "call_migrate.py", "cli_call_consumer.py", "test_v5.py")
HELPER = [
    ("laptop_post.py", 'if not DEDUP_ENFORCED or op.get("retried"):', 'if op.get("retried"):'),
    ("laptop_post.py", '    txn.save()                                             # on disk BEFORE the append', '    pass'),
    ("laptop_post.py", '    elif seen is None:\n        return "UNKNOWN"', '    elif seen is None:\n        pass'),
    ("laptop_post.py", '    return True if same else "CONFLICT"', '    return True'),
    ("laptop_post.py", '    seen = find_row(op_id, records=reread(), sha=sha)', '    seen = True'),
    ("laptop_post.py", '        row = op["row"]                                    # the row as first intended, byte for byte',
     '        pass'),
    ("call_state.py", "                if clock() > end:", "                if True:\n                    break\n                if clock() > end:"),
    ("call_state.py", "    opened = [t[\"entry\"] for t in state[\"tasks\"].values() if t[\"state\"] in OPEN and t.get(\"entry\")]",
     "    opened = []"),
    ("call_state.py", '        if missing_ok:\n            return empty()', '        if True:\n            return empty()'),
    ("call_state.py", '    return min(points, key=sid) if points else "0-0"',
     '    return min(points, key=sid) if points else "%d-0" % int(time.time() * 1000)'),
    ("call_state.py", '        if ask in txn.state["tasks"]:\n            return False', '        if False:\n            return False'),
    ("call_ask.py", 'if st["state"] != "open" or st.get("agent") != TAG:', 'if False:'),
    ("call_ask.py", 'return ("OK" if hashlib.sha256(words.encode("utf-8")).hexdigest() == digest else "MISMATCH"), words',
     'return "OK", words'),
    ("call_ask.py", '            if st is None:\n                print("UNKNOWN_AUTH: no status',
     '            if False:\n                print("UNKNOWN_AUTH: no status'),
    ("call_ask.py", '            if st["state"] != "open":\n                print(begin_cancel(ask, task, txn))\n                return 2\n            if task["state"] == "ACCEPTED":',
     '            if task["state"] == "ACCEPTED":'),
    ("call_ask.py", "            if mine and canonical(mine[0]) == task[\"hash\"]:", "            if mine:"),
    ("call_ask.py", 'if item is None or card["type"] != task.get("expect"):', 'if item is None:'),
    ("call_ask.py", '    if not task.get("ops", {}).get("CLAUDE-CARD-%s" % ask):', '    if True:'),
    ("call_ask.py", '    if card_at is None:\n        if now()', '    if False:\n        if now()'),
    ("call_ask.py", '        if wd_at > card_at:', '        if True:'),
    ("call_ask.py", '        if now() - task["origin"] > MAX_AGE:\n            cs.touch(task, state="FAILED_CARD_MAY_BE_LIVE")\n        return task["state"]\n    for wd',
     '        return task["state"]\n    for wd'),
    ("call_ask.py", 'if st is not None and st["state"] == "cancelled" and', 'if False and'),
    ("call_ask.py", 'extra=["status_op=" + srid]', 'extra=[]'),
    ("call_ask.py", '            if task["rev"] != rev:\n                print("RE-PLAN")\n                return 6\n            if st is None:\n                print("UNKNOWN_AUTH: no status',
     '            if st is None:\n                print("UNKNOWN_AUTH: no status'),
    ("call_ask.py", '    stamp = utc().strftime("%Y%m%dT%H%M%S") + os.urandom(2).hex()', '    stamp = "FIXED"'),
    ("call_ask.py", '    if os.path.exists(cs.STATE):\n', '    if False:\n'),
    ("call_ask.py", '    if a.verb == "migrate":\n        return migrate(a.legacy)', '    pass'),
    ("call_migrate.py", '            st = "HISTORICAL_CLOSED"', '            st = "DONE"'),
    ("call_migrate.py", '    if pending and pending not in state["tasks"]:', '    if False:'),
    ("call_migrate.py", 'if cs.sid(old) > cs.sid(last) or cs.sid(old) > cs.sid(new):', 'if False:'),
    ("cli_call_consumer.py", '            if t["state"] == "PENDING":\n                return ask', '            if False:\n                return ask'),
]
BRIDGE = [
    ('pipe.set(SAID_KEY % ask, digest, ex=TTL_SECONDS)', 'pass'),
    ('                pipe.set(SAID_PART_KEY % (ask, index), json.dumps(part, ensure_ascii=False), ex=TTL_SECONDS)',
     '                pipe.set(SAID_PART_KEY % (ask, index), json.dumps(part[:-1], ensure_ascii=False), ex=TTL_SECONDS)'),
    ('SAID_PART_CHARS = 90 ', 'SAID_PART_CHARS = 150 '),
    ('            if state.get("state") != "open" or state.get("conference_room") != room:',
     '            if state.get("state") != "open":'),
    ('            if named and state.get("agent") not in named:', '            if False:'),
    ('            if (now - claimed_at).total_seconds() > PENDING_SECONDS or claimed_at > now:', '            if False:'),
    ('            pipe.hdel(OPEN_KEY, ask)\n            pipe.set(STATE_KEY % ask, json.dumps({"state": "cancelled"',
     '            pipe.set(STATE_KEY % ask, json.dumps({"state": "cancelled"'),
    ('            if isinstance(mine, dict) and mine.get("activity") in BRIDGE_ACTIVITIES and mine.get("status") == "claimed":',
     '            if True:'),
    ('                self.cancel_latest(room, named_in(said), entry_id, now)       # 6b: accepted work too', '                pass'),
    ('            pipe.set(STATE_KEY % ask, json.dumps({"state": "done", "agent": agent, "at": stamp(now)}, sort_keys=True),\n                     ex=TTL_SECONDS)',
     '            pass'),
]


def run_one(name, old, new, copy, test_cmd, cwd):
    path = os.path.join(copy, name)
    src = open(path, encoding="utf-8").read()
    if src.count(old) != 1:
        return "BAD ANCHOR"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(src.replace(old, new))
    r = subprocess.run(test_cmd, cwd=cwd, capture_output=True, text=True, timeout=600,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return "killed" if r.returncode != 0 else "SURVIVED"


def main():
    results = []
    for i, (name, old, new) in enumerate(HELPER, 1):
        tmp = tempfile.mkdtemp()
        for f in FILES:
            shutil.copy(os.path.join(HERE, f), tmp)
        env_note = os.environ.setdefault("CLI_HELPER_BLACKBOARD", REPO)
        results.append(("H%d" % i, name, run_one(name, old, new, tmp, [sys.executable, "test_v5.py"], tmp)))
        shutil.rmtree(tmp, ignore_errors=True)
    for i, (old, new) in enumerate(BRIDGE, 1):
        tmp = tempfile.mkdtemp()
        shutil.copytree(os.path.join(REPO, "scripts"), os.path.join(tmp, "scripts"))
        shutil.copytree(os.path.join(REPO, "tests"), os.path.join(tmp, "tests"))
        cloud = os.path.join(REPO, "cloud", "bus-reconciler")
        shutil.copytree(cloud, os.path.join(tmp, "cloud", "bus-reconciler"))
        cmd = [sys.executable, "-m", "unittest", "tests.test_bridge_6ab", "tests.test_call_bridge"]
        results.append(("B%d" % i, "scripts/call_bridge.py",
                        run_one(os.path.join("scripts", "call_bridge.py"), old, new, tmp, cmd, tmp)))
        shutil.rmtree(tmp, ignore_errors=True)
    for tag, name, verdict in results:
        print("%-4s %-9s %s" % (tag, verdict, name))
    bad = [t for t, _, v in results if v != "killed"]
    print("%d/%d killed; not killed: %s" % (len(results) - len(bad), len(results), bad or "none"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
