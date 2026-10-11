"""The broken-telephone deploy scripts: what they say must match what the code reads, and what they
must never say (a public service, all-traffic egress, a flag on by default, a secret value).

A deploy script drifts from its code silently: a renamed env var in main.py and the job keeps
running with the default. So every `environ.get("NAME")` the two services read must be set by the
script that deploys them, or be on the short list of optional ones, and nothing else.
"""
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
INTAKE = REPO / "cloud" / "drive-intake"
WORKER = REPO / "cloud" / "claude-api-worker"
# environ.get("NAME") and the two helpers that take the name as a string. A helper call is still a
# read: _int_env(environ, "CAP") is how the cap, the reservation, the heartbeat and the attempt
# limit are read, and a regex that only sees environ.get misses every one of them.
ENV_READ = re.compile(
    r'(?:environ\.get\(\s*"([A-Z0-9_]+)"|environ\[\s*"([A-Z0-9_]+)"\s*\]'
    r'|(?:_int_env|_float_env|_utc_deadline_env)\(\s*environ\s*,\s*"([A-Z0-9_]+)")'
)

# Read by the code, deliberately not set by the deploy: the wake ring until #359 lands, and Cloud
# Run's own PORT. Nothing on the worker's side: since #374 an unset price, rate, input bound or
# deadline means the worker calls nothing, so every one of them is set by the script (fail-closed
# defaults, pinned below) and read back by `worker on`.
OPTIONAL = {
    "drive-intake": {"INBOX_WAKE_URL", "INBOX_WAKE_SECRET", "PORT", "DRIVE_INTAKE_BIND"},
    "claude-api-worker": set(),
}
COST_INPUTS = ("CLAUDE_API_WORKER_USD_PER_MTOK_IN", "CLAUDE_API_WORKER_USD_PER_MTOK_OUT", "FLEET_CAD_PER_USD",
               "CLAUDE_API_WORKER_MAX_INPUT_TOKENS", "CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC")
# Pinned as well as scanned. These are the names deps_from_env wires for a real deploy (#370).
# The scan above catches a rename in main.py; this set catches a script that drops one.
WIRED_BY_370 = {
    "DRIVE_INTAKE_FOLDER_ID", "DRIVE_INTAKE_CHANNEL_TOKEN", "DRIVE_INTAKE_POLL_TOKEN",
    "DRIVE_INTAKE_API_WORKER", "DRIVE_INTAKE_HEARTBEAT_SECONDS", "DRIVE_INTAKE_POLL_SECONDS",
}
# Named by the script but read through a mount or a secret rather than environ.get in main.py.
VIA_SECRET_OR_MOUNT = {"ANTHROPIC_API_KEY", "BUS_URL", "BUS_SECRET"}


def env_names_read(*paths):
    names = set()
    for path in paths:
        for groups in ENV_READ.findall(path.read_text(encoding="utf-8")):
            names.update(group for group in groups if group)
    return names


def function_body(script, name):
    match = re.search(r"(?m)^%s\(\) \{\n(.*?)\n\}" % re.escape(name), script, re.S)
    if not match:
        raise AssertionError("no function %s" % name)
    return match.group(1)


def commands(body):
    """The lines of a function that run: a comment that names a verb is not a call of it."""
    return "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))


def env_names_set(script_text):
    """Every NAME= inside --set-env-vars, --update-env-vars and --set-secrets, plus the flag verbs."""
    names = set()
    for block in re.findall(r'--(?:set|update)-env-vars "([^"]+)"', script_text):
        names |= {part.split("=", 1)[0] for part in block.split(",") if "=" in part}
    for block in re.findall(r'--set-secrets "([^"]+)"', script_text):
        names |= {part.split("=", 1)[0] for part in block.split(",") if "=" in part and not part.startswith("/")}
    names |= set(re.findall(r'flag ([A-Z_]+) ', script_text))
    return names


def quoted(script_text, name):
    """The single-quoted python a script keeps in NAME='...'."""
    return script_text.split(name + "='\n", 1)[1].split("\n'\n", 1)[0]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_bash():
    """A bash that can run a script from this tree, or None.

    On Windows the first bash on a plain PATH is System32's, the WSL launcher. It runs in another
    filesystem, where C:/... is no path at all, and where WSL is absent or denied it answers every
    call with an error: all twelve shell tests failed on it. Git for Windows keeps its own bash
    beside git and off PATH. That one drives these tests; the launcher never does.
    """
    found = shutil.which("bash")
    if os.name != "nt":
        return found

    def launcher(path):
        folded = path.lower().replace("/", "\\")
        return "\\system32\\" in folded or "\\windowsapps\\" in folded

    if found and not launcher(found):
        return found
    git = shutil.which("git")
    for root in list(Path(git).resolve().parents)[:3] if git else []:
        for candidate in (root / "usr" / "bin" / "bash.exe", root / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


BASH = find_bash()
NO_BASH = "no bash here that can run a script from this tree (the WSL launcher is not one)"


def shell_env(**extra):
    """The environment a script is run in: bash's own directory first, so a bash found beside git
    rather than on PATH still finds the sed, grep and mktemp that live next to it. Nothing these
    tests stand in for is found through PATH: gcloud, curl and python are each handed over by name."""
    path = os.environ.get("PATH", "")
    if BASH:
        path = str(Path(BASH).parent) + os.pathsep + path
    return dict(os.environ, PATH=path, **extra)


def gate_constant(name, source="COST_MISSING_PY"):
    """A constant of one of the worker script's gates, read by running the gate's own source."""
    namespace = {"__name__": "gate"}
    exec(quoted((WORKER / "deploy.sh").read_text(encoding="utf-8"), source), namespace)
    return namespace[name]


def gate():
    """The worker script's cost gate, as python: problems(env, now)."""
    namespace = {"__name__": "cost_gate"}
    exec(quoted((WORKER / "deploy.sh").read_text(encoding="utf-8"), "COST_MISSING_PY"), namespace)
    return namespace["problems"]


class IntakeDeployTest(unittest.TestCase):
    script = (INTAKE / "deploy.sh").read_text(encoding="utf-8")

    def test_every_env_the_service_reads_is_set_or_declared_optional(self):
        read = env_names_read(INTAKE / "main.py")
        read -= {"DRIVE_INTAKE_ENABLED"}                          # set by the script too, checked below
        missing = read - env_names_set(self.script) - OPTIONAL["drive-intake"]
        self.assertEqual(set(), missing)
        self.assertEqual(set(), WIRED_BY_370 - env_names_set(self.script))
        self.assertIn("DRIVE_INTAKE_ENABLED=off", self.script)
        self.assertIn("REDIS_CONNECT=true", self.script)
        self.assertIn("REDIS_HOST=${REDIS_HOST}", self.script)

    def test_private_service_private_egress_flag_off_no_values(self):
        self.assertIn("--no-allow-unauthenticated", self.script)
        self.assertNotIn("--allow-unauthenticated", self.script.replace("--no-allow-unauthenticated", ""))
        self.assertIn('EGRESS="private-ranges-only"', self.script)
        self.assertNotIn('EGRESS="all-traffic"', self.script)
        self.assertNotIn("--vpc-egress all-traffic", self.script)
        self.assertIn("--oidc-service-account-email", self.script)
        self.assertIn("X-Drive-Intake-Poll-Token=", self.script)   # the header main.py reads
        self.assertNotIn("REDIS_HOST=10.", self.script)            # the private address is never committed
        self.assertNotIn("token_hex(32)\" >", self.script)         # a minted token goes to Secret Manager, not a file
        for secret in ("REDIS_AUTH_STRING", "REDIS_CA_CERT", "DRIVE_INTAKE_POLL_TOKEN", "DRIVE_INTAKE_CHANNEL_TOKEN"):
            self.assertIn(secret, self.script)
        self.assertNotIn("ANTHROPIC_API_KEY", self.script)         # intake never holds the model key
        self.assertRegex(self.script, r"services enable[^\n]*docs\.googleapis\.com[^\n]*drive\.googleapis\.com")
        self.assertNotIn("print-identity-token --audiences", self.script)   # a user account's token has none
        self.assertIn("DRIVE_INTAKE_RING=${RING}", self.script)             # the polled ring, by name
        self.assertIn('RING="${DRIVE_INTAKE_RING:-polled}"', self.script)
        # The poller never attempts without this, and a second schedule must update the header.
        self.assertIn("roles/iam.serviceAccountTokenCreator", self.script)
        self.assertIn("service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com", self.script)
        self.assertNotIn("v1/namespaces", self.script)
        self.assertIn('header_flag="--headers"', self.script)
        self.assertIn('header_flag="--update-headers"', self.script)
        tokens = function_body(self.script, "tokens")
        self.assertIn("grant_secret", tokens)
        self.assertNotIn("continue", tokens)                      # an existing secret still gets granted

    def test_trail_reads_the_doc_as_the_service_does_and_never_prints_the_token(self):
        trail = function_body(self.script, "trail")
        # The id is checked against the shape main.py accepts BEFORE anything is read or sent. Pinned
        # to the literal, and to FILE_ID_OK itself once the /trail route (#376) is on this branch.
        self.assertIn("DOC_ID_OK='^[A-Za-z0-9_-]{10,128}$'", self.script)
        whole = '[[ "$doc" =~ $DOC_ID_OK ]]'                     # the whole argument; grep answers per line
        self.assertLess(trail.index(whole), trail.index("service_url"))
        self.assertLess(trail.index(whole), trail.index("secrets versions access"))
        self.assertNotIn("grep", commands(trail))
        self.assertIn('"$CURL" -sS --fail-with-body', trail)      # an HTTP refusal is not a proof
        # curl by name and never through PATH: Git for Windows' bash puts its own curl in front of
        # the stand-in, and three tests then sent the real request (found on f011d08, 2026-10-11).
        self.assertIn('CURL="${CURL:-curl}"', self.script)
        self.assertNotRegex(commands(self.script), r"(?m)^\s*curl\b")
        main_text = (INTAKE / "main.py").read_text(encoding="utf-8")
        file_id_ok = re.search(r'FILE_ID_OK = re\.compile\(r"([^"]+)"\)', main_text)
        if file_id_ok:
            self.assertEqual("[A-Za-z0-9_-]{10,128}", file_id_ok.group(1).replace("\\Z", ""))
        self.assertIn('"${url}/trail?doc=${doc}"', trail)
        self.assertIn('-H "X-Drive-Intake-Poll-Token: ${token}"', trail)        # the header main.py reads
        self.assertIn('Authorization: Bearer $("$GCLOUD" auth print-identity-token)', trail)
        self.assertIn("secret DRIVE_INTAKE_POLL_TOKEN", trail)
        self.assertNotRegex(trail, r"echo[^\n]*token")                           # read, sent, never printed
        self.assertEqual(1, trail.count("token="), trail)                        # one assignment; the header uses it; nothing else
        self.assertEqual(1, trail.count("${token}"), trail)
        self.assertIn('trail) trail "$@" ;;', self.script)
        self.assertIn("deploy.sh trail <doc id>", self.script)

    def test_studio_grants_the_controller_and_prints_the_two_settings_but_never_releases_it(self):
        studio = function_body(self.script, "studio")
        self.assertIn('STUDIO_SERVICE="${STUDIO_SERVICE:-sfdc24-studio-controller}"', self.script)
        self.assertIn("value(spec.template.spec.serviceAccountName)", studio)
        self.assertIn('${PROJECT_NUMBER}-compute@developer.gserviceaccount.com', studio)   # the fallback when unset
        self.assertRegex(studio, r'run services add-iam-policy-binding "\$SERVICE"[^\n]*\n[^\n]*roles/run\.invoker')
        self.assertRegex(studio, r'secrets add-iam-policy-binding DRIVE_INTAKE_POLL_TOKEN[^\n]*\n[^\n]*secretAccessor')
        self.assertIn("STUDIO_TELEPHONE_URL=${url}", studio)
        self.assertIn("STUDIO_TELEPHONE_TOKEN=DRIVE_INTAKE_POLL_TOKEN:latest", studio)   # the secret's name, not its value
        self.assertNotIn("secrets versions access", studio)                             # the token is never read here
        for verb in ("run services update", "run deploy", "update-traffic", "builds submit"):
            self.assertNotIn(verb, commands(studio))                                     # the release is the runbook's, his
        self.assertIn("studio) studio ;;", self.script)

    def test_on_refuses_while_the_worker_pilot_intake_is_on(self):
        flag = function_body(self.script, "flag")
        self.assertIn('WORKER_JOB="${WORKER_JOB:-claude-api-worker}"', self.script)
        self.assertLess(flag.index('grep -qx "PHONE_DOC_INTAKE_ENABLED=on"'), flag.index("run services update"))
        self.assertIn('if ! other="$(env_lines jobs "$WORKER_JOB")"; then', flag)   # unreadable is a refusal
        self.assertIn("exit 2", flag)
        env_lines = function_body(self.script, "env_lines")
        self.assertIn("--format json", env_lines)
        self.assertIn('"$PYTHON" -c "$ENV_LINES_PY"', env_lines)
        self.assertNotIn("|| true", env_lines)                    # a failed read is never an empty answer
        self.assertNotIn("2>/dev/null", env_lines)
        # One reader, two scripts: the worker's copy must be this one, to the byte.
        worker = (WORKER / "deploy.sh").read_text(encoding="utf-8")
        self.assertEqual(env_lines, function_body(worker, "env_lines"))
        self.assertEqual(quoted(self.script, "ENV_LINES_PY"), quoted(worker, "ENV_LINES_PY"))


class WorkerDeployTest(unittest.TestCase):
    script = (WORKER / "deploy.sh").read_text(encoding="utf-8")

    def test_every_env_the_job_reads_is_set_or_declared_optional(self):
        read = env_names_read(WORKER / "main.py", WORKER / "lane.py", WORKER / "phone_intake.py")
        self.assertIn("PHONE_DOC_PILOT_TARGET", env_names_read(WORKER / "phone_intake.py"))   # read as environ[...]
        read -= {"CLAUDE_API_WORKER_ENABLED", "CLAUDE_API_LANE_ENABLED"}
        missing = read - env_names_set(self.script) - OPTIONAL["claude-api-worker"] - VIA_SECRET_OR_MOUNT
        self.assertEqual(set(), missing)
        self.assertIn("CLAUDE_API_WORKER_ENABLED=off", self.script)
        self.assertIn("CLAUDE_API_LANE_ENABLED=off", self.script)
        self.assertIn("PHONE_DOC_INTAKE_ENABLED=off", self.script)
        # A deploy leaves no pilot behind: the flag off and the target empty (#386), in the deploy itself.
        self.assertIn("PHONE_DOC_INTAKE_ENABLED=off,PHONE_DOC_PILOT_TARGET=,", function_body(self.script, "deploy"))
        self.assertIn("REDIS_CONNECT=true", self.script)
        self.assertIn("REDIS_HOST=${REDIS_HOST}", self.script)

    def test_his_decisions_are_env_and_the_model_id_is_never_committed(self):
        self.assertIn("FLEET_SPEND_CAP_CENTS_CAD:-2500", self.script)
        self.assertIn("CLAUDE_API_WORKER_RESERVE_CENTS_CAD:-5", self.script)
        self.assertIn("DRIVE_INTAKE_HEARTBEAT_SECONDS:-300", self.script)
        self.assertIn("CLAUDE_API_WORKER_MAX_ATTEMPTS:-1", self.script)
        self.assertIn('MODEL="${CLAUDE_API_WORKER_MODEL:-}"', self.script)
        self.assertNotRegex(self.script, r"claude-(haiku|sonnet|opus)-\d")
        self.assertIn("--max-retries 0", self.script)             # one attempt, no automatic retry
        self.assertRegex(self.script, r"services enable[^\n]*docs\.googleapis\.com[^\n]*drive\.googleapis\.com")
        self.assertIn('EGRESS="private-ranges-only"', self.script)
        self.assertNotIn('EGRESS="all-traffic"', self.script)
        self.assertNotIn("--vpc-egress all-traffic", self.script)
        for secret in ("ANTHROPIC_API_KEY", "BUS_URL", "BUS_SECRET"):
            self.assertIn(secret + "=" + secret + ":latest", self.script)          # env secrets
        for secret in ("REDIS_AUTH_STRING", "REDIS_CA_CERT"):
            self.assertIn("=" + secret + ":latest", self.script)                   # mounted as files
        self.assertNotIn("REDIS_AUTH_STRING=REDIS_AUTH_STRING", self.script)       # never as an env var
        # v2 :run, and TokenCreator on the ticker. The v1 namespaces form needs run.developer; the
        # other role leaves the ticker at code -1 with no lastAttemptTime. Comments may name the
        # wrong form; the URI that is assigned may not.
        uris = re.findall(r'^RUN_URI="([^"]+)"', self.script, re.M)
        self.assertEqual(
            ["https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"],
            uris)
        self.assertIn("roles/iam.serviceAccountTokenCreator", self.script)
        self.assertNotIn("roles/iam.serviceAccountUser", self.script)
        # The readback names the job's fields in BOTH shapes. This test used to forbid the v1 one;
        # on gcloud 584 the v1 shape is what `run jobs describe` answers, and the v2-only projection
        # printed nothing for the real job (measured 2026-10-11).
        readback = function_body(self.script, "readback")
        for field in ("spec.template.spec.template.spec.serviceAccountName", "template.template.serviceAccount",
                      "spec.template.spec.template.spec.containers[0].env", "template.template.containers[0].env",
                      "spec.template.metadata.annotations", "template.template.vpcAccess"):
            self.assertIn(field, readback)

    def test_the_reconcilers_readback_names_both_shapes_too(self):
        # The projection the worker's readback was copied from. It printed one line for the real
        # bus-reconciler job on gcloud 584; the v1 projection printed 27 (measured 2026-10-11).
        reconciler = function_body((REPO / "cloud" / "bus-reconciler" / "deploy.sh").read_text(encoding="utf-8"), "readback")
        for field in ("spec.template.spec.template.spec.containers[0].env", "template.template.containers[0].env",
                      "spec.template.spec.template.spec.serviceAccountName", "template.template.serviceAccount"):
            self.assertIn(field, reconciler)

    def test_cost_bounds_are_wired_but_fail_closed_by_default(self):
        for name, var, default in (("CLAUDE_API_WORKER_MAX_INPUT_TOKENS", "MAX_INPUT_TOKENS", "0"),
                                   ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "AUTHORIZED_UNTIL_UTC", ""),
                                   ("CLAUDE_API_WORKER_USD_PER_MTOK_IN", "PRICE_IN", "0"),
                                   ("CLAUDE_API_WORKER_USD_PER_MTOK_OUT", "PRICE_OUT", "0"),
                                   ("FLEET_CAD_PER_USD", "CAD_PER_USD", "0")):
            self.assertIn("${%s:-%s}" % (name, default), self.script)
            self.assertIn(name + "=${" + var + "}", self.script)
        # The scan above sees the deadline too: main.py reads it through its own helper.
        self.assertIn("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", env_names_read(WORKER / "main.py"))

    def test_worker_on_reads_the_job_back_and_refuses_while_a_cost_input_is_unset(self):
        # Fail-closed defaults deploy a worker that calls nothing and says QUEUED once a day per
        # task. The flag verb reads the JOB back (not this shell) and refuses before the flag.
        self.assertIn("worker) case \"${2:-}\" in on) worker_on ;; off) flag CLAUDE_API_WORKER_ENABLED off ;;", self.script)
        worker_on = function_body(self.script, "worker_on")
        self.assertLess(worker_on.index("cost_missing"), worker_on.index("flag CLAUDE_API_WORKER_ENABLED on"))
        self.assertIn("exit 2", worker_on)
        cost_missing = function_body(self.script, "cost_missing")
        self.assertIn('lines="$(env_lines jobs "$JOB")" || return 1', cost_missing)
        self.assertIn('"$PYTHON" -c "$COST_MISSING_PY"', cost_missing)
        self.assertIn('if ! missing="$(cost_missing)"; then', worker_on)             # unreadable is a refusal
        for name in COST_INPUTS + ("CLAUDE_API_WORKER_MODEL",):
            self.assertIn('"%s"' % name, quoted(self.script, "COST_MISSING_PY"), name)
        self.assertIn("cost) cost_readback ;;", self.script)
        self.assertIn("deploy.sh cost", self.script)
        env_lines = function_body(self.script, "env_lines")
        self.assertIn("--format json", env_lines)
        self.assertNotIn("--format 'yaml", env_lines)

    def test_the_pilot_intake_is_a_flag_that_refuses_while_the_service_intake_is_on(self):
        self.assertIn("pilot) case \"${2:-}\" in on) pilot_on ;; off) pilot_off ;;", self.script)
        pilot_on = function_body(self.script, "pilot_on")
        update = pilot_on.index("run jobs update")
        self.assertIn('INTAKE_SERVICE="${INTAKE_SERVICE:-drive-intake}"', self.script)
        self.assertLess(pilot_on.index('grep -qx "DRIVE_INTAKE_ENABLED=on"'), update)
        self.assertIn('env_lines services "$INTAKE_SERVICE"', pilot_on)
        self.assertIn("exit 2", pilot_on)
        self.assertIn("pilot on|off", self.script)
        # #386: one reviewed request or nothing. The target is judged before anything is read or
        # sent, the flag and the target travel in ONE update, and the job is read back after it.
        self.assertLess(pilot_on.index('"$PYTHON" -c "$PILOT_TARGET_PY"'), pilot_on.index("env_lines"))
        self.assertLess(pilot_on.index('grep -qx "CLAUDE_API_LANE_ENABLED=on"'), update)
        self.assertEqual(1, commands(pilot_on).count("run jobs update"))
        self.assertNotIn("flag PHONE_DOC_INTAKE_ENABLED on", self.script)        # never the flag without its target
        # The target reaches gcloud in a file and on no command line: gcloud.cmd breaks on a quote
        # in an argument and cmd.exe eats the carets of ^@^ (measured, `gcloud meta test --arg-dict`).
        self.assertIn('--region "$REGION" --flags-file "$flags" || status=$?', pilot_on)
        self.assertIn('flags="$(cygpath -m "$flags")"', pilot_on)                # a Windows gcloud does not know /tmp
        self.assertIn('"$PYTHON" -c "$PILOT_TARGET_PY" flags | tr -d "\\r" >"$flags"', pilot_on)
        self.assertNotIn("^@^", commands(self.script))
        for line in commands(self.script).split("\n"):
            if "$GCLOUD" in line or "--update-env-vars" in line or "--set-env-vars" in line:
                self.assertNotRegex(line, r"\$\{?(target|PHONE_DOC_PILOT_TARGET)\b", line)
        self.assertLess(update, pilot_on.index('rm -f "$flags"'))
        self.assertLess(update, pilot_on.index('grep -qxF "PHONE_DOC_PILOT_TARGET=${target}"'))
        self.assertLess(pilot_on.index('grep -qxF "PHONE_DOC_PILOT_TARGET=${target}"'), pilot_on.index("pilot_off >&2"))
        # Off is one update, with no read before it, and it never leaves a worker on to read the whole inbox.
        pilot_off = function_body(self.script, "pilot_off")
        self.assertNotIn("env_lines", commands(pilot_off))
        self.assertIn('--update-env-vars "PHONE_DOC_INTAKE_ENABLED=off,CLAUDE_API_WORKER_ENABLED=off,PHONE_DOC_PILOT_TARGET="',
                      pilot_off)
        lane_on = function_body(self.script, "lane_on")
        self.assertIn("lane) case \"${2:-}\" in on) lane_on ;; off) flag CLAUDE_API_LANE_ENABLED off ;;", self.script)
        self.assertLess(lane_on.index("PHONE_DOC_INTAKE_ENABLED=on|PHONE_DOC_PILOT_TARGET=.+"),
                        lane_on.index("flag CLAUDE_API_LANE_ENABLED on"))


class ShellTest(unittest.TestCase):
    def test_the_bash_that_drives_these_tests_is_never_the_wsl_launcher(self):
        # shutil.which("bash") on a plain Windows PATH is C:\Windows\System32\bash.exe. Every shell
        # test here failed on it (2026-10-11), and none of them was about the script.
        if os.name != "nt":
            self.skipTest("a Windows trap")
        if BASH:
            self.assertNotIn("\\system32\\", BASH.lower().replace("/", "\\"))
            self.assertNotIn("\\windowsapps\\", BASH.lower().replace("/", "\\"))
            said = subprocess.run([BASH, "-c", "printf %s ran"], capture_output=True, text=True, env=shell_env())
            self.assertEqual("ran", said.stdout, said.stderr)

    def test_both_scripts_parse_in_bash_and_show_usage_without_a_verb(self):
        bash = BASH
        if not bash:
            self.skipTest(NO_BASH)
        for script in (INTAKE / "deploy.sh", WORKER / "deploy.sh"):
            self.assertEqual(0, subprocess.run([bash, "-n", str(script)], capture_output=True, env=shell_env()).returncode,
                             script)
            usage = subprocess.run([bash, str(script)], capture_output=True, text=True, cwd=str(REPO), env=shell_env())
            self.assertEqual(2, usage.returncode, script)
            self.assertIn("deploy.sh setup", usage.stdout, script)
            self.assertNotIn("gcloud", usage.stderr, script)    # usage never calls gcloud
            # The usage is the header, printed by line range: a verb added to the header must still print.
            last_verb = "deploy.sh trail <doc id>" if script.parent == INTAKE else "deploy.sh pilot off"
            self.assertIn(last_verb, usage.stdout, script)
            self.assertNotIn("set -euo pipefail", usage.stdout, script)

    def test_a_bad_doc_id_never_reaches_gcloud_and_a_good_one_does(self):
        bash = BASH
        if not bash:
            self.skipTest(NO_BASH)
        # gcloud is a path that does not exist: a call reaching it fails loudly, by name.
        env = shell_env(GCLOUD="/nonexistent/gcloud")
        for bad in ("", "short", "x" * 129, "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP/../x", "a b c d e f g h i j"):
            run = subprocess.run([bash, str(INTAKE / "deploy.sh"), "trail", bad], capture_output=True, text=True,
                                 cwd=str(REPO), env=env)
            self.assertEqual(2, run.returncode, bad)
            self.assertIn("usage: deploy.sh trail", run.stderr, bad)
            self.assertNotIn("nonexistent", run.stderr, bad)    # refused before any gcloud call
        # The positive control: a well-shaped id passes the check and the next thing is gcloud.
        run = subprocess.run([bash, str(INTAKE / "deploy.sh"), "trail", "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"],
                             capture_output=True, text=True, cwd=str(REPO), env=env)
        self.assertNotEqual(2, run.returncode)
        self.assertNotIn("usage: deploy.sh trail", run.stderr)
        self.assertIn("nonexistent", run.stderr)


FAKE_GCLOUD = r'''#!/usr/bin/env bash
# A gcloud that logs every call and answers describe with the JSON it is given. Nothing else runs.
# A --flags-file is logged as what the file holds: that is what gcloud would read, and the file is
# gone by the time a test looks. A file that cannot be read fails the call, as gcloud's does.
line="$*"
previous=""
for word in "$@"; do
  if [ "$previous" = "--flags-file" ]; then
    held="$(cat "$word")" || { echo "ERROR: (gcloud) --flags-file [$word] not found." >&2; exit 1; }
    line="${line/"$word"/"$held"}"
  fi
  previous="$word"
done
printf '%s\n' "$line" >> "$FAKE_LOG"
case "$*" in
  *"--flags-file"*)
    if [ -n "${FAKE_FLAGS_UPDATE_FAILS:-}" ]; then echo "ERROR: (gcloud.run.jobs.update) ABORTED: Conflict for resource" >&2; exit 1; fi ;;
esac
case "$*" in
  *"value(status.url)"*) echo "https://drive-intake.invalid" ;;
  *"serviceAccountName"*) printf '%s\n' "$FAKE_STUDIO_SA" ;;
  *"run jobs describe"*"--format json"*)
    # What the job holds once an update has been sent, when a test says what that is.
    if [ -n "${FAKE_JOB_AFTER:-}" ] && grep -q "^run jobs update" "$FAKE_LOG"; then
      describe "${FAKE_JOB_AFTER_FAILS:-}" "$FAKE_JOB_AFTER" job
    else
      describe "${FAKE_JOB_FAILS:-}" "$FAKE_JOB_JSON" job
    fi ;;
  *"run services describe"*"--format json"*) describe "${FAKE_SERVICE_FAILS:-}" "$FAKE_SERVICE_JSON" service ;;
  *"secrets versions access"*) printf '%s' "tok-123" ;;
  *"auth print-identity-token"*) printf '%s' "idt-456" ;;
esac
'''
# How a describe goes wrong, in gcloud's own words: absent, denied, down, or an answer that is not
# JSON. And three answers that ARE JSON and still cannot be read as lines: a resource with its
# containers in no place the reader knows (shapeless), one with two containers (twins), and one
# whose variable is there twice (twice).
FAKE_DESCRIBE = r'''describe() {
  case "$1" in
    notfound) echo "ERROR: (gcloud.run.describe) Cannot find $3 [x]" >&2; exit 1 ;;
    denied) echo "ERROR: (gcloud.run.describe) PERMISSION_DENIED: Permission denied on resource x (or it may not exist). NOT_FOUND is not what this says" >&2; exit 1 ;;
    down) echo "ERROR: (gcloud.run.describe) UNAVAILABLE: The service is currently unavailable." >&2; exit 1 ;;
    garbage) printf '%s' "Updates are available for some Google Cloud CLI components." ;;
    list) printf '%s' "[]" ;;
    shapeless) printf '%s' '{"apiVersion":"run.googleapis.com/v9","kind":"Job","spec":{"somewhere":{"else":{"containers":[{"env":[]}]}}}}' ;;
    twins) printf '%s' "$2" | sed 's/"containers": \[/"containers": [{"env": []}, /' ;;
    twice) printf '%s' "$2" | sed 's/"env": \[/"env": [{"name": "PHONE_DOC_INTAKE_ENABLED", "value": "off"}, {"name": "DRIVE_INTAKE_ENABLED", "value": "off"}, /' ;;
    *) printf '%s' "$2" ;;
  esac
}
'''
FAKE_GCLOUD = FAKE_GCLOUD.replace('line="$*"', FAKE_DESCRIBE + 'line="$*"', 1)
# curl as curl behaves: an HTTP error is exit 0 unless --fail-with-body asked for 22.
FAKE_CURL = r'''#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_CURL_HTTP:-}" ]; then
  printf '%s' '{"error":"not found"}'
  case "$*" in *--fail-with-body*) exit 22 ;; esac
  exit 0
fi
printf '%s' '{"status":"ok","enabled":true}'
'''
# A python that writes CRLF into the pipe, as Windows CPython does, on any platform.
FAKE_CRLF_PYTHON = r'''#!/usr/bin/env bash
"$REAL_PYTHON" "$@" | sed 's/$/\r/'
exit "${PIPESTATUS[0]}"
'''
# A secret the job holds, as the job really lists it: no value, a source. It is never a line.
SECRET_ITEM = {"name": "ANTHROPIC_API_KEY", "valueFrom": {"secretKeyRef": {"key": "latest", "name": "ANTHROPIC_API_KEY"}}}


def env_list(values, extra=()):
    """The env list of a describe answer: literals from {name: value}, then any raw items."""
    return [{"name": name, "value": value} for name, value in (values or {}).items()] + list(extra)


def job_json(values=None, extra=(), shape="v1"):
    """A job as `gcloud run jobs describe --format json` answers.

    v1 IS THE REAL ONE. Its key structure was read from the real job on 2026-10-11 (gcloud 584;
    every value here is made up): the env four levels down, egress in an annotation, a secret as
    valueFrom. Until that day this stand-in answered in the v2 shape, the only one the reader knew,
    so every test passed while the reader read nothing from the real job. v2 (template.template)
    is what the v2 API answers and stays as the second shape."""
    container = {"image": "us-central1-docker.pkg.dev/p/r/claude-api-worker:v1", "env": env_list(values, extra) + [SECRET_ITEM]}
    if shape == "v2":
        return json.dumps({"name": "projects/p/locations/r/jobs/claude-api-worker",
                           "template": {"taskCount": 1, "template": {"containers": [container], "maxRetries": 0}}})
    return json.dumps({
        "apiVersion": "run.googleapis.com/v1", "kind": "Job", "metadata": {"name": "claude-api-worker"},
        "spec": {"template": {
            "metadata": {"annotations": {"run.googleapis.com/vpc-access-egress": "private-ranges-only"}},
            "spec": {"parallelism": 1, "taskCount": 1, "template": {"spec": {
                "containers": [container], "maxRetries": 0, "serviceAccountName": "sa", "timeoutSeconds": "600"}}}}},
        "status": {}})


def service_json(values=None, extra=()):
    """A service as `gcloud run services describe --format json` answers (serving.knative.dev/v1; the
    place of its containers was read from a real service on 2026-10-11)."""
    return json.dumps({"apiVersion": "serving.knative.dev/v1", "kind": "Service", "metadata": {"name": "drive-intake"},
                       "spec": {"template": {"spec": {"containers": [{"env": env_list(values, extra)}],
                                                      "serviceAccountName": "sa"}}},
                       "status": {"url": "https://drive-intake.invalid"}})


DOC = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"
SET = dict(CLAUDE_API_WORKER_MODEL="m", CLAUDE_API_WORKER_USD_PER_MTOK_IN="0.1", CLAUDE_API_WORKER_USD_PER_MTOK_OUT="0.5",
           FLEET_CAD_PER_USD="1.45", CLAUDE_API_WORKER_MAX_INPUT_TOKENS="100000",
           CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC="2099-01-01T00:00:00Z",
           FLEET_SPEND_CAP_CENTS_CAD="2500", CLAUDE_API_WORKER_RESERVE_CENTS_CAD="5", CLAUDE_API_WORKER_MAX_TOKENS="1500")
DEFAULTS = dict(SET, CLAUDE_API_WORKER_USD_PER_MTOK_IN="0", CLAUDE_API_WORKER_USD_PER_MTOK_OUT="0",
                FLEET_CAD_PER_USD="0", CLAUDE_API_WORKER_MAX_INPUT_TOKENS="0", CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC="")
NOW = 1_760_000_000.0                                              # 2025-10-09, inside SET's deadline
# Present, and still nothing the worker can use. The value on the right is what main.py reads it as.
UNUSABLE = (
    ("CLAUDE_API_WORKER_USD_PER_MTOK_IN", "-0.1"), ("CLAUDE_API_WORKER_USD_PER_MTOK_IN", "nan"),
    ("CLAUDE_API_WORKER_USD_PER_MTOK_OUT", "inf"), ("CLAUDE_API_WORKER_USD_PER_MTOK_OUT", "0.5 USD"),
    ("FLEET_CAD_PER_USD", "-1.45"), ("FLEET_CAD_PER_USD", "1,45"), ("FLEET_CAD_PER_USD", "1e-400"),
    ("CLAUDE_API_WORKER_MAX_INPUT_TOKENS", "100000.0"), ("CLAUDE_API_WORKER_MAX_INPUT_TOKENS", "-5"),
    ("CLAUDE_API_WORKER_MAX_INPUT_TOKENS", "100k"), ("CLAUDE_API_WORKER_MAX_INPUT_TOKENS", "1500"),
    ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2099-01-01T00:00:00+00:00"),
    ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2099-01-01T00:00:00z"),
    ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2099-01-01"),
    ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2099-13-01T00:00:00Z"),
    ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2020-01-01T00:00:00Z"),          # well-formed, and past
    ("FLEET_SPEND_CAP_CENTS_CAD", "0"), ("FLEET_SPEND_CAP_CENTS_CAD", "25.00"),
    ("CLAUDE_API_WORKER_RESERVE_CENTS_CAD", "0"), ("CLAUDE_API_WORKER_MAX_TOKENS", "0"),
    ("CLAUDE_API_WORKER_MODEL", "  "),
)


# One reviewed request, as the review hands it over: any key order, any spacing.
TARGET = {"task_id": "task-0123456789abcdef", "source_doc_id": "17Q2vBYvxs8PIu-sXXWj67HAU_DL2RfB9-SoQbpeDoWQ",
          "recipient_id": "claude-api", "request_id": "REQ-20261011-01", "source_revision": "a" * 64}
# The one line the script makes of it and the job is given: the worker's own field order, no space.
TARGET_LINE = ('{"source_doc_id":"17Q2vBYvxs8PIu-sXXWj67HAU_DL2RfB9-SoQbpeDoWQ","source_revision":"%s",'
               '"request_id":"REQ-20261011-01","task_id":"task-0123456789abcdef","recipient_id":"claude-api"}' % ("a" * 64))
PILOT_SENT = {"PHONE_DOC_INTAKE_ENABLED": "on", "PHONE_DOC_PILOT_TARGET": TARGET_LINE}
PILOT_OFF_UPDATE = "--update-env-vars PHONE_DOC_INTAKE_ENABLED=off,CLAUDE_API_WORKER_ENABLED=off,PHONE_DOC_PILOT_TARGET="


def pilot_sent(call):
    """What a `pilot on` update hands gcloud: the env in its flags file, or None for any other call.
    The whole call is checked: the target rides in the file and nowhere on the command line."""
    head, flags, held = call.partition(" --flags-file ")
    if not flags or not head.startswith("run jobs update claude-api-worker ") or "env-vars" in head:
        return None
    sent = json.loads(held)
    return sent["--update-env-vars"] if list(sent) == ["--update-env-vars"] else None


class FakeGcloudTest(unittest.TestCase):
    """The verbs that read a deployed resource back, driven end to end against a fake gcloud (and a
    fake curl) that log every call: what each verb refuses, what it then runs, what it never prints.
    The string tests above pin the text; this is the path."""

    @classmethod
    def setUpClass(cls):
        cls.bash = BASH
        if not cls.bash:
            raise unittest.SkipTest(NO_BASH)
        cls.tmp = Path(tempfile.mkdtemp(prefix="fake-gcloud-"))
        for name, text in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL), ("crlf-python", FAKE_CRLF_PYTHON)):
            (cls.tmp / name).write_text(text, encoding="utf-8", newline="\n")
            os.chmod(cls.tmp / name, 0o755)
        cls.calls = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def verb(self, script, *args, job=None, service=None, after=None, shape="v1", job_extra=(), service_extra=(),
             studio_sa="studio-sa@sfdc24.iam.gserviceaccount.com", **fakes):
        type(self).calls += 1
        log = self.tmp / ("calls-%d.log" % self.calls)
        if after is not None:
            fakes["FAKE_JOB_AFTER"] = job_json(after, shape=shape)
        # gcloud AND curl by name. Neither is left to PATH: the bash Git for Windows ships puts its
        # own directories first, and a stand-in found through PATH is then not the one that runs.
        env = shell_env(GCLOUD=(self.tmp / "gcloud").as_posix(), CURL=(self.tmp / "curl").as_posix(),
                        FAKE_LOG=log.as_posix(), FAKE_STUDIO_SA=studio_sa,
                        FAKE_JOB_JSON=job_json(job, job_extra, shape),
                        FAKE_SERVICE_JSON=service_json(service, service_extra), **fakes)
        if "PHONE_DOC_PILOT_TARGET" not in fakes:
            env.pop("PHONE_DOC_PILOT_TARGET", None)              # only a test that names a target has one
        run = subprocess.run([self.bash, str(script), *args], capture_output=True, text=True, cwd=str(REPO), env=env)
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return run, calls

    def pilot_on(self, target=TARGET, held=TARGET_LINE, flag="on", job=None, **fakes):
        """`pilot on` with a target in the shell; `held` and `flag` are what the job holds afterwards."""
        after = dict(job or {}, PHONE_DOC_INTAKE_ENABLED=flag, PHONE_DOC_PILOT_TARGET=held)
        if target is not None:
            fakes["PHONE_DOC_PILOT_TARGET"] = target if isinstance(target, str) else json.dumps(target, indent=1)
        return self.verb(WORKER / "deploy.sh", "pilot", "on", job=job, after=after, **fakes)

    def test_worker_on_refuses_the_fail_closed_defaults_and_flips_once_they_are_set(self):
        run, calls = self.verb(WORKER / "deploy.sh", "worker", "on", job=DEFAULTS)
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertIn("would call nothing", run.stderr)
        for name in COST_INPUTS:
            self.assertIn(name, run.stderr)
        self.assertNotIn("CLAUDE_API_WORKER_MODEL", run.stderr)                      # set, so not named
        self.assertEqual([], [c for c in calls if "run jobs update" in c])            # the flag never moved
        run, calls = self.verb(WORKER / "deploy.sh", "worker", "on", job=SET)
        self.assertEqual(0, run.returncode, run.stderr)
        updates = [c for c in calls if c.startswith("run jobs update claude-api-worker")]
        self.assertEqual(1, len(updates), calls)
        self.assertIn("--update-env-vars CLAUDE_API_WORKER_ENABLED=on", updates[0])
        run, calls = self.verb(WORKER / "deploy.sh", "worker", "on", job={})            # no job deployed at all
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertEqual([], [c for c in calls if "run jobs update" in c])

    def test_cost_prints_what_is_deployed_and_names_what_is_missing(self):
        run, _ = self.verb(WORKER / "deploy.sh", "cost", job=dict(DEFAULTS, CLAUDE_API_WORKER_USD_PER_MTOK_IN="0.1"))
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn("CLAUDE_API_WORKER_USD_PER_MTOK_IN=0.1", run.stdout)
        self.assertIn("missing or unusable on claude-api-worker:", run.stdout)
        missing = run.stdout.split("missing or unusable on claude-api-worker:", 1)[1]
        self.assertIn("CLAUDE_API_WORKER_USD_PER_MTOK_OUT", missing)
        self.assertIn("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", missing)
        self.assertNotIn("CLAUDE_API_WORKER_USD_PER_MTOK_IN", missing)
        run, _ = self.verb(WORKER / "deploy.sh", "cost", job=SET)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn("every cost input on claude-api-worker is one the worker can use", run.stdout)

    def test_the_two_intakes_refuse_each_other_and_each_flips_when_the_other_is_off(self):
        run, calls = self.verb(INTAKE / "deploy.sh", "on", job=dict(PHONE_DOC_INTAKE_ENABLED="on"))
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertIn("one intake at a time", run.stderr)
        self.assertEqual([], [c for c in calls if "run services update" in c])
        run, calls = self.verb(INTAKE / "deploy.sh", "on", job=dict(PHONE_DOC_INTAKE_ENABLED="off"))
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(1, len([c for c in calls if c.startswith("run services update drive-intake")
                                 and "DRIVE_INTAKE_ENABLED=on" in c]), calls)
        run, calls = self.verb(INTAKE / "deploy.sh", "off", job=dict(PHONE_DOC_INTAKE_ENABLED="on"))   # off is always allowed
        self.assertEqual(0, run.returncode, run.stderr)
        run, calls = self.pilot_on(service=dict(DRIVE_INTAKE_ENABLED="on"))
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertIn("one intake at a time", run.stderr)
        self.assertEqual([], [c for c in calls if "run jobs update" in c])
        run, calls = self.pilot_on(service=dict(DRIVE_INTAKE_ENABLED="off"))
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(1, len([c for c in calls if pilot_sent(c) == PILOT_SENT]), calls)
        run, calls = self.pilot_on(service={})                                            # no service: nothing to collide with
        self.assertEqual(0, run.returncode, run.stderr)

    def test_pilot_on_needs_the_one_request_and_sends_it_with_the_flag_in_one_update(self):
        # #386: the flag alone leaves a job that stops on every tick (pilot_target_invalid). f011d08
        # sent the flag alone. No target in the shell is a refusal before anything is read or sent.
        run, calls = self.pilot_on(target=None)
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertIn("PHONE_DOC_PILOT_TARGET: empty", run.stderr)
        self.assertIn("pilot on needs PHONE_DOC_PILOT_TARGET", run.stderr)
        self.assertEqual([], calls)
        for bad, why in ((dict(TARGET, source_doc_id="1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"), "not one of the two originals"),
                         (dict(TARGET, recipient_id="claude-code-cli"), "recipient_id is not claude-api"),
                         (dict(TARGET, task_id="task-XYZ"), "task_id is not in the shape"),
                         ({k: v for k, v in TARGET.items() if k != "source_revision"}, "not an object of exactly"),
                         ("{not json", "not JSON")):
            run, calls = self.pilot_on(target=bad)
            self.assertEqual(2, run.returncode, (bad, run.stderr))
            self.assertIn(why, run.stderr, bad)
            self.assertEqual([], calls, bad)
        # The good one, handed over with its keys in another order and spread over lines: one
        # update, the flag and the target together, the target as the one line the job will hold.
        run, calls = self.pilot_on()
        self.assertEqual(0, run.returncode, run.stderr)
        updates = [c for c in calls if "run jobs update" in c]
        self.assertEqual(1, len(updates), calls)
        self.assertEqual(PILOT_SENT, pilot_sent(updates[0]), updates)
        self.assertEqual(TARGET, json.loads(TARGET_LINE))
        self.assertIn("for the one request " + TARGET_LINE, run.stdout)
        self.assertIn("the worker is off: nothing is called until worker on", run.stdout)
        self.assertLess(calls.index(updates[0]), max(i for i, c in enumerate(calls) if "run jobs describe" in c))   # read back after
        run, calls = self.pilot_on(job=dict(CLAUDE_API_WORKER_ENABLED="on"))
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertNotIn("the worker is off", run.stdout)

    def test_pilot_on_turns_the_pilot_off_again_when_the_job_does_not_hold_what_was_sent(self):
        # A quote lost between this shell and gcloud (gcloud.cmd is a batch file) leaves a target the
        # worker refuses on every tick. The job is read back, and a pilot that cannot be confirmed
        # is turned off again, the worker with it.
        lost_quotes = TARGET_LINE.replace('"', "")
        doc_id = TARGET["source_doc_id"]
        for held, flag, how in ((lost_quotes, "on", ""), (TARGET_LINE + " ", "on", ""), ("", "on", ""),
                                (TARGET_LINE, "off", ""), (TARGET_LINE, "on", "denied"), (TARGET_LINE, "on", "garbage"),
                                (TARGET_LINE, "on", "shapeless"), (TARGET_LINE, "on", "twins"),
                                # Cursor on b975708. Each of these read back as "held": the value was
                                # judged a line at a time, and carriage returns were deleted first.
                                # The worker refuses every one (pilot_target_invalid, pilot_flags_invalid).
                                (TARGET_LINE + "\nGARBAGE", "on", ""), ("\n" + TARGET_LINE, "on", ""),
                                (TARGET_LINE + "\r", "on", ""),
                                (TARGET_LINE.replace(doc_id, doc_id[:5] + "\r" + doc_id[5:]), "on", ""),
                                (TARGET_LINE, "on\r", ""), (TARGET_LINE, "on\nEXTRA", ""), (TARGET_LINE, "on ", "")):
            run, calls = self.pilot_on(held=held, flag=flag, FAKE_JOB_AFTER_FAILS=how)
            self.assertEqual(1, run.returncode, (held, flag, how, run.stderr))
            self.assertIn("the pilot goes off again", run.stderr)
            updates = [c for c in calls if "run jobs update" in c]
            self.assertEqual(2, len(updates), calls)
            self.assertEqual(PILOT_SENT, pilot_sent(updates[0]), updates)
            self.assertTrue(updates[1].endswith(PILOT_OFF_UPDATE), updates)
            self.assertNotIn("for the one request", run.stdout)
        # An update gcloud reports as failed is not proof that nothing was written: off again, unread.
        run, calls = self.pilot_on(FAKE_FLAGS_UPDATE_FAILS="1")
        self.assertEqual(1, run.returncode, run.stderr)
        self.assertIn("gcloud exit 1", run.stderr)
        updates = [c for c in calls if "run jobs update" in c]
        self.assertEqual(2, len(updates), calls)
        self.assertTrue(updates[1].endswith(PILOT_OFF_UPDATE), updates)
        self.assertEqual([], [c for c in calls[calls.index(updates[0]):] if "describe" in c], calls)
        self.assertNotIn("for the one request", run.stdout)

    def test_pilot_off_is_one_update_with_no_read_and_takes_the_worker_with_it(self):
        # With the pilot off and the target cleared, a worker left on reads the whole inbox; with
        # the target left behind it stops on every tick. And an off verb never waits on a read.
        for fails in ("", "denied", "down"):
            run, calls = self.verb(WORKER / "deploy.sh", "pilot", "off", job=dict(CLAUDE_API_WORKER_ENABLED="on"),
                                   FAKE_JOB_FAILS=fails, FAKE_SERVICE_FAILS=fails)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertEqual(1, len(calls), calls)
            self.assertTrue(calls[0].startswith("run jobs update claude-api-worker "), calls)
            self.assertTrue(calls[0].endswith(PILOT_OFF_UPDATE), calls)
            self.assertIn("CLAUDE_API_WORKER_ENABLED=off", run.stdout)

    def test_the_lane_and_the_pilot_refuse_each_other(self):
        # #386: both on, or the lane on beside a target that is still set, is pilot_flags_invalid
        # on every tick. Each on verb reads the job back and refuses; an unreadable job is a refusal.
        for job in (dict(PHONE_DOC_INTAKE_ENABLED="on", PHONE_DOC_PILOT_TARGET=TARGET_LINE),
                    dict(PHONE_DOC_INTAKE_ENABLED="on"),
                    dict(PHONE_DOC_INTAKE_ENABLED="off", PHONE_DOC_PILOT_TARGET=TARGET_LINE),
                    # Cursor on b975708: a target whose FIRST LINE is empty read as no target, and
                    # the lane went on beside a value main() still treats as set.
                    dict(PHONE_DOC_INTAKE_ENABLED="off", PHONE_DOC_PILOT_TARGET="\n"),
                    dict(PHONE_DOC_INTAKE_ENABLED="off", PHONE_DOC_PILOT_TARGET="\r"),
                    dict(PHONE_DOC_INTAKE_ENABLED="off", PHONE_DOC_PILOT_TARGET="\n" + TARGET_LINE),
                    dict(PHONE_DOC_INTAKE_ENABLED="off", PHONE_DOC_PILOT_TARGET=" ")):
            run, calls = self.verb(WORKER / "deploy.sh", "lane", "on", job=job)
            self.assertEqual(2, run.returncode, (job, run.stderr))
            self.assertIn("pilot off first", run.stderr)
            self.assertEqual([], [c for c in calls if "run jobs update" in c], job)
        for how in ("denied", "down", "garbage", "list", "shapeless", "twins"):
            run, calls = self.verb(WORKER / "deploy.sh", "lane", "on", FAKE_JOB_FAILS=how)
            self.assertEqual(2, run.returncode, how + run.stderr)
            self.assertIn("could not be read back", run.stderr, how)
            self.assertEqual([], [c for c in calls if "run jobs update" in c], how)
            run, calls = self.pilot_on(FAKE_JOB_FAILS=how)
            self.assertEqual(2, run.returncode, how + run.stderr)
            self.assertIn("could not be read back", run.stderr, how)
            self.assertEqual([], [c for c in calls if "run jobs update" in c], how)
        # A cleared target is no target, however the answer writes an empty string: "" or no value at all.
        for job, extra in ((dict(PHONE_DOC_INTAKE_ENABLED="off"), ()),
                           (dict(PHONE_DOC_INTAKE_ENABLED="off", PHONE_DOC_PILOT_TARGET=""), ()),
                           (dict(PHONE_DOC_INTAKE_ENABLED="off"), ({"name": "PHONE_DOC_PILOT_TARGET"},))):
            run, calls = self.verb(WORKER / "deploy.sh", "lane", "on", job=job, job_extra=extra)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertEqual(1, len([c for c in calls if c.endswith("--update-env-vars CLAUDE_API_LANE_ENABLED=on")]), calls)
        run, calls = self.verb(WORKER / "deploy.sh", "lane", "off", FAKE_JOB_FAILS="denied")     # off never waits on a read
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(1, len(calls), calls)
        run, calls = self.pilot_on(job=dict(CLAUDE_API_LANE_ENABLED="on"))
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertIn("lane off first", run.stderr)
        self.assertEqual([], [c for c in calls if "run jobs update" in c])

    def test_the_verbs_read_the_job_in_the_shape_this_gcloud_answers_and_in_the_v2_one(self):
        # THE TEST THAT WAS MISSING. `gcloud run jobs describe --format json` on gcloud 584 answers
        # in the v1 shape; the reader knew only v2 and read nothing from the real job, so worker on
        # refused a good job and drive-intake on would have gone on beside a pilot. The stand-in
        # answered v2, so nothing here showed it. verb() now answers v1 unless told otherwise.
        for shape in ("v1", "v2"):
            run, calls = self.verb(WORKER / "deploy.sh", "worker", "on", job=SET, shape=shape)
            self.assertEqual(0, run.returncode, (shape, run.stderr))                       # a good job is READ as good
            self.assertEqual(1, len([c for c in calls if c.endswith("--update-env-vars CLAUDE_API_WORKER_ENABLED=on")]), calls)
            run, calls = self.verb(WORKER / "deploy.sh", "cost", job=SET, shape=shape)
            self.assertIn("CLAUDE_API_WORKER_USD_PER_MTOK_IN=0.1", run.stdout, shape)
            self.assertNotIn("ANTHROPIC_API_KEY", run.stdout, shape)                       # a secret is never a line
            run, calls = self.verb(INTAKE / "deploy.sh", "on", job=dict(PHONE_DOC_INTAKE_ENABLED="on"), shape=shape)
            self.assertEqual(2, run.returncode, (shape, run.stderr))                       # the pilot is SEEN
            self.assertIn("one intake at a time", run.stderr, shape)
            self.assertEqual([], [c for c in calls if "run services update" in c], shape)
            run, calls = self.verb(WORKER / "deploy.sh", "lane", "on", job=dict(PHONE_DOC_INTAKE_ENABLED="on"), shape=shape)
            self.assertEqual(2, run.returncode, (shape, run.stderr))
            run, calls = self.pilot_on(shape=shape)
            self.assertEqual(0, run.returncode, (shape, run.stderr))

    def test_a_job_the_lines_cannot_speak_for_is_unreadable_and_every_on_verb_refuses(self):
        # A name there twice (the first says off, the second on), and a flag that comes from a
        # secret, which no line can show: neither is "off".
        from_a_secret = {"name": "PHONE_DOC_INTAKE_ENABLED", "valueFrom": {"secretKeyRef": {"key": "latest", "name": "x"}}}
        for fakes in (dict(job=dict(PHONE_DOC_INTAKE_ENABLED="on"), FAKE_JOB_FAILS="twice"), dict(job_extra=(from_a_secret,))):
            run, calls = self.verb(INTAKE / "deploy.sh", "on", **fakes)
            self.assertEqual(2, run.returncode, (fakes, run.stderr))
            self.assertIn("could not be read back", run.stderr, fakes)
            self.assertEqual([], [c for c in calls if "run services update" in c], fakes)
            run, calls = self.verb(WORKER / "deploy.sh", "lane", "on", **fakes)
            self.assertEqual(2, run.returncode, (fakes, run.stderr))
            self.assertEqual([], [c for c in calls if "run jobs update" in c], fakes)
        run, calls = self.pilot_on(service=dict(DRIVE_INTAKE_ENABLED="on"), FAKE_SERVICE_FAILS="twice")
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertIn("could not be read back", run.stderr)
        self.assertEqual([], [c for c in calls if "run jobs update" in c])

    def test_a_value_with_a_line_break_in_it_cannot_pass_for_another_variable(self):
        # One variable, one line. A second line of some other value used to be read as a variable
        # of its own, and the last one won: a zero price read as a good one, and worker on flipped.
        smuggled = dict(SET, CLAUDE_API_WORKER_USD_PER_MTOK_IN="0", ZZ_NOTE="x\nCLAUDE_API_WORKER_USD_PER_MTOK_IN=0.1")
        run, calls = self.verb(WORKER / "deploy.sh", "worker", "on", job=smuggled)
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertIn("CLAUDE_API_WORKER_USD_PER_MTOK_IN", run.stderr)
        self.assertEqual([], [c for c in calls if "run jobs update" in c])
        # And a flag is on only when it is exactly on, as each service reads its own.
        for value in ("on\r", "on\nEXTRA", " on", "ON"):
            run, calls = self.verb(INTAKE / "deploy.sh", "on", job=dict(PHONE_DOC_INTAKE_ENABLED=value))
            self.assertEqual(0, run.returncode, (value, run.stderr))
        run, calls = self.verb(INTAKE / "deploy.sh", "on", job=dict(ZZ_NOTE="x\nPHONE_DOC_INTAKE_ENABLED=on"))
        self.assertEqual(0, run.returncode, run.stderr)                                    # a note is not the flag

    def test_studio_grants_the_discovered_account_and_prints_the_url_never_the_token(self):
        run, calls = self.verb(INTAKE / "deploy.sh", "studio")
        self.assertEqual(0, run.returncode, run.stderr)
        member = "--member serviceAccount:studio-sa@sfdc24.iam.gserviceaccount.com"
        self.assertEqual(1, len([c for c in calls if c.startswith("run services add-iam-policy-binding drive-intake")
                                 and member in c and "--role roles/run.invoker" in c]), calls)
        self.assertEqual(1, len([c for c in calls if c.startswith("secrets add-iam-policy-binding DRIVE_INTAKE_POLL_TOKEN")
                                 and member in c and "secretmanager.secretAccessor" in c]), calls)
        self.assertIn("--update-env-vars STUDIO_TELEPHONE_URL=https://drive-intake.invalid "
                      "--update-secrets STUDIO_TELEPHONE_TOKEN=DRIVE_INTAKE_POLL_TOKEN:latest", run.stdout)
        self.assertNotIn("tok-123", run.stdout + run.stderr)
        self.assertEqual([], [c for c in calls if "secrets versions access" in c or "run services update" in c
                              or "run deploy" in c or "update-traffic" in c])
        run, calls = self.verb(INTAKE / "deploy.sh", "studio", studio_sa="")              # unset: the default compute account
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(1, len([c for c in calls if "serviceAccount:96522051727-compute@developer.gserviceaccount.com" in c
                                 and "roles/run.invoker" in c]), calls)

    def test_trail_sends_both_headers_to_the_route_and_prints_only_the_answer(self):
        run, calls = self.verb(INTAKE / "deploy.sh", "trail", DOC)
        self.assertEqual(0, run.returncode, run.stderr)
        curls = [c for c in calls if c.startswith("curl ")]
        self.assertEqual(1, len(curls), calls)
        self.assertIn("-H Authorization: Bearer idt-456", curls[0])
        self.assertIn("-H X-Drive-Intake-Poll-Token: tok-123", curls[0])
        self.assertIn("https://drive-intake.invalid/trail?doc=" + DOC, curls[0])   # .invalid: no stand-in gone missing can reach a host
        self.assertEqual('{"status":"ok","enabled":true}\n', run.stdout)
        self.assertNotIn("tok-123", run.stdout)

    def test_trail_on_an_http_refusal_prints_the_body_and_fails(self):
        # Until #376 is deployed the route is a 404. curl -s exits 0 on that, and the proof command
        # then "succeeds" on a missing route (Cursor and Copilot on 9eced7e).
        run, calls = self.verb(INTAKE / "deploy.sh", "trail", DOC, FAKE_CURL_HTTP="404")
        self.assertEqual(1, run.returncode, run.stderr)
        self.assertIn('{"error":"not found"}', run.stdout)                           # what the service said
        self.assertIn("NOT read", run.stderr)
        self.assertIn("not a proof", run.stderr)
        self.assertNotIn("tok-123", run.stdout + run.stderr)

    def test_a_doc_id_with_a_good_line_in_it_never_reaches_gcloud(self):
        # The newline is made inside bash (printf %b): a Windows parent cuts an argument at one, and
        # the script would then be handed the good line alone.
        script = (INTAKE / "deploy.sh").as_posix()
        for bad in ("bad\\n" + DOC, DOC + "\\nbad", DOC + "\\n", "\\n" + DOC):
            run, calls = self.verb("-c", 'printf -v doc "%b" "$1"; exec bash "$0" trail "$doc"', script, bad)
            self.assertEqual(2, run.returncode, repr(bad))
            self.assertIn("usage: deploy.sh trail", run.stderr, repr(bad))
            self.assertEqual([], calls, repr(bad))
        run, calls = self.verb("-c", 'printf -v doc "%b" "$1"; exec bash "$0" trail "$doc"', script, DOC)   # the control
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(1, len([c for c in calls if c.startswith("curl ")]), calls)

    def test_an_interlock_that_cannot_read_the_other_side_refuses(self):
        # A denied describe, an outage and an answer that is not the resource all used to print
        # nothing, and nothing read as "the other intake is off" (Cursor and Copilot on 9eced7e).
        for how in ("denied", "down", "garbage", "list", "shapeless", "twins"):
            run, calls = self.verb(INTAKE / "deploy.sh", "on", job=dict(PHONE_DOC_INTAKE_ENABLED="on"), FAKE_JOB_FAILS=how)
            self.assertEqual(2, run.returncode, how + run.stderr)
            self.assertIn("could not be read back", run.stderr, how)
            self.assertEqual([], [c for c in calls if "run services update" in c], how)
            run, calls = self.pilot_on(service=dict(DRIVE_INTAKE_ENABLED="on"), FAKE_SERVICE_FAILS=how)
            self.assertEqual(2, run.returncode, how + run.stderr)
            self.assertIn("could not be read back", run.stderr, how)
            self.assertEqual([], [c for c in calls if "run jobs update" in c], how)
            run, calls = self.verb(WORKER / "deploy.sh", "worker", "on", job=SET, FAKE_JOB_FAILS=how)
            self.assertEqual(2, run.returncode, how + run.stderr)
            self.assertEqual([], [c for c in calls if "run jobs update" in c], how)
        run, _ = self.verb(WORKER / "deploy.sh", "cost", job=SET, FAKE_JOB_FAILS="denied")
        self.assertEqual(1, run.returncode, run.stderr)
        self.assertNotIn("worker on is allowed", run.stdout)
        # gcloud's own words reach the terminal: the reason is not swallowed with the answer.
        self.assertIn("PERMISSION_DENIED", run.stderr)
        # Absent is the one failure that IS "off": nothing there to collide with.
        run, calls = self.verb(INTAKE / "deploy.sh", "on", FAKE_JOB_FAILS="notfound")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(1, len([c for c in calls if "run services update" in c and "DRIVE_INTAKE_ENABLED=on" in c]), calls)
        run, calls = self.pilot_on(FAKE_SERVICE_FAILS="notfound")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(1, len([c for c in calls if pilot_sent(c) == PILOT_SENT]), calls)
        run, calls = self.verb(WORKER / "deploy.sh", "worker", "on", FAKE_JOB_FAILS="notfound")    # no job: nothing to turn on
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertEqual([], [c for c in calls if "run jobs update" in c])

    def test_the_interlock_holds_when_python_writes_crlf_into_the_pipe(self):
        # Windows CPython writes \r\n, and grep -x does not match "NAME=on\r": both verbs turned
        # their flag on beside the other intake (Cursor on 9eced7e). Any platform, by a wrapper.
        crlf = dict(PYTHON=(self.tmp / "crlf-python").as_posix(), REAL_PYTHON=sys.executable.replace("\\", "/"))
        run, calls = self.verb(INTAKE / "deploy.sh", "on", job=dict(PHONE_DOC_INTAKE_ENABLED="on"), **crlf)
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertIn("one intake at a time", run.stderr)
        self.assertEqual([], [c for c in calls if "run services update" in c])
        run, calls = self.pilot_on(service=dict(DRIVE_INTAKE_ENABLED="on"), **crlf)
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertIn("one intake at a time", run.stderr)
        self.assertEqual([], [c for c in calls if "run jobs update" in c])
        # The target is printed by that python too: a carriage return left on it would be sent to
        # the job inside the value, and the read-back would then never find the line it sent.
        run, calls = self.pilot_on(**crlf)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(1, len([c for c in calls if pilot_sent(c) == PILOT_SENT]), calls)
        run, calls = self.verb(WORKER / "deploy.sh", "lane", "on", job=dict(PHONE_DOC_INTAKE_ENABLED="on"), **crlf)
        self.assertEqual(2, run.returncode, run.stderr)
        run, calls = self.verb(WORKER / "deploy.sh", "worker", "on", job=SET, **crlf)        # and a good job still flips
        self.assertEqual(0, run.returncode, run.stderr)
        run, calls = self.verb(WORKER / "deploy.sh", "worker", "on", job=DEFAULTS, **crlf)
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertNotIn("\r", run.stderr)

    def test_worker_on_refuses_values_the_worker_would_read_as_nothing(self):
        # Present is not usable (Cursor and Copilot on 9eced7e): each of these deployed, flipped the
        # flag, and left every task with the daily QUEUED "nothing called" line.
        for name, value in (("CLAUDE_API_WORKER_USD_PER_MTOK_IN", "-0.1"),
                            ("CLAUDE_API_WORKER_MAX_INPUT_TOKENS", "100000.0"),
                            ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2099-01-01T00:00:00+00:00"),
                            ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2020-01-01T00:00:00Z")):
            run, calls = self.verb(WORKER / "deploy.sh", "worker", "on", job=dict(SET, **{name: value}))
            self.assertEqual(2, run.returncode, (name, value, run.stderr))
            self.assertIn(name, run.stderr)
            self.assertEqual([], [c for c in calls if "run jobs update" in c], (name, value))


class CostGateTest(unittest.TestCase):
    """The gate in `worker on` against the worker it gates. A value the gate passes must be one
    main.py's own readers turn into a reservation; the gate may be stricter, never looser."""

    @classmethod
    def setUpClass(cls):
        cls.problems = staticmethod(gate())
        cls.wk = load("claude_api_worker_for_deploy", WORKER / "main.py")

    def smallest_request(self, model, max_tokens):
        """What call_reservation measures against the input bound, for a request of no words at all."""
        wk = self.wk
        request = json.dumps({"model": model, "max_tokens": max_tokens, "system": wk.DOCTRINE % wk.RESULT_MAX,
                              "messages": [{"role": "user", "content": ""}]}).encode("utf-8")
        return len(request) + wk.INPUT_TOKEN_OVERHEAD

    def runtime_can_call(self, env, now):
        """main.py's verdict on this env, by its own readers and the conditions call_reservation states:
        whether ANY request, even an empty one, could be reserved and called."""
        wk = self.wk
        until = wk._utc_deadline_env(env, "CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC")
        max_tokens = wk._int_env(env, "CLAUDE_API_WORKER_MAX_TOKENS", wk.DEFAULT_MAX_TOKENS)
        return bool(
            (env.get("CLAUDE_API_WORKER_MODEL") or "").strip()
            and wk._int_env(env, "CLAUDE_API_WORKER_RESERVE_CENTS_CAD", 0) > 0
            and wk._int_env(env, "FLEET_SPEND_CAP_CENTS_CAD", 0) > 0
            and max_tokens > 0
            and wk._int_env(env, "CLAUDE_API_WORKER_MAX_INPUT_TOKENS", 0)
            >= self.smallest_request(env.get("CLAUDE_API_WORKER_MODEL") or "", max_tokens)
            and until is not None and now < until
            and wk.valid_prices(wk._float_env(env, "CLAUDE_API_WORKER_USD_PER_MTOK_IN"),
                                wk._float_env(env, "CLAUDE_API_WORKER_USD_PER_MTOK_OUT"),
                                wk._float_env(env, "FLEET_CAD_PER_USD")))

    def test_the_good_set_passes_both_and_the_defaults_fail_both(self):
        self.assertEqual([], self.problems(dict(SET), NOW))
        self.assertTrue(self.runtime_can_call(dict(SET), NOW))
        named = " ".join(self.problems(dict(DEFAULTS), NOW))
        for name in COST_INPUTS:
            self.assertIn(name, named)
        self.assertFalse(self.runtime_can_call(dict(DEFAULTS), NOW))
        self.assertEqual([], self.problems({k: v for k, v in SET.items() if k != "CLAUDE_API_WORKER_MAX_TOKENS"}, NOW))

    def test_an_input_bound_too_small_for_the_workers_own_instructions_is_refused(self):
        # Cursor on fb22006: a whole number under about 1877 passed the gate, and call_reservation
        # still returned nothing, because the instructions and the overhead alone are that long.
        floor = gate_constant("MIN_INPUT_BOUND")
        at = lambda bound: dict(SET, CLAUDE_API_WORKER_MAX_INPUT_TOKENS=str(bound))  # noqa: E731
        self.assertEqual([], self.problems(at(floor), NOW))
        found = self.problems(at(floor - 1), NOW)
        self.assertEqual(1, len(found), found)
        self.assertTrue(found[0].startswith("CLAUDE_API_WORKER_MAX_INPUT_TOKENS (under %d" % floor), found)
        # The floor stays above main.py's own, with room for a real request: a long model id and
        # the largest answer limit do not lift the smallest request to half of it. A doctrine
        # that grows past that fails HERE, not in a doc that says QUEUED once a day.
        smallest = self.smallest_request("m" * 64, 10 ** 7)
        self.assertGreater(smallest, 1500)                               # the measurement is of something
        self.assertLessEqual(smallest * 2, floor)
        self.assertTrue(self.runtime_can_call(at(floor), NOW))
        self.assertFalse(self.runtime_can_call(at(1500), NOW))

    def test_every_unusable_value_is_named_and_none_of_them_would_have_called(self):
        for name, value in UNUSABLE:
            env = dict(SET, **{name: value})
            found = self.problems(env, NOW)
            self.assertEqual(1, len(found), (name, value, found))
            self.assertTrue(found[0].startswith(name + " ("), (name, value, found))
            # The control: main.py reads the same value as nothing it can call with. A row in the
            # table that the worker WOULD call with is a gate refusing a good deploy.
            self.assertFalse(self.runtime_can_call(env, NOW), (name, value))

    def test_the_gate_is_never_looser_than_the_worker(self):
        # Every cell the gate passes, the worker must pass. Wider than the table above: shapes the
        # two readers could disagree on (spaces, signs, exponents, fractions of a second, offsets).
        prices = ("0.1", " 0.1 ", "+0.1", "1e-3", "1E2", ".5", "5.", "0x10", "1_0", "", "0", "0.0", "-0", "NaN", "Infinity")
        ints = ("100000", " 100000 ", "+7", "1_000", "07", "1e5", "100000.0", "", "0", "-1", "0x10",
                "1876", "1877", "4095", "4096", "+4096", "4_096")
        dates = ("2099-01-01T00:00:00Z", "2099-01-01T00:00:00.000Z", "2099-01-01 00:00:00Z", "20990101T000000Z",
                 "2099-01-01T00:00Z", "2099-01-01T00:00:00ZZ", "Z", "", "2099-01-01T00:00:00+00:00Z",
                 "2025-10-09T08:53:20Z", "2025-10-09T08:53:21Z")
        cells = [(n, v) for n in ("CLAUDE_API_WORKER_USD_PER_MTOK_IN", "CLAUDE_API_WORKER_USD_PER_MTOK_OUT", "FLEET_CAD_PER_USD")
                 for v in prices]
        cells += [(n, v) for n in ("CLAUDE_API_WORKER_MAX_INPUT_TOKENS", "FLEET_SPEND_CAP_CENTS_CAD",
                                   "CLAUDE_API_WORKER_RESERVE_CENTS_CAD", "CLAUDE_API_WORKER_MAX_TOKENS") for v in ints]
        cells += [("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", v) for v in dates]
        passed = 0
        for name, value in cells:
            env = dict(SET, **{name: value})
            if not self.problems(env, NOW):
                passed += 1
                self.assertTrue(self.runtime_can_call(env, NOW), (name, value))
        self.assertGreater(passed, 20)                                # the sweep is not vacuous
        self.assertTrue(math.isfinite(NOW))
        # The deadline is exclusive, as the worker's is: at the second itself nothing is called.
        at = dict(SET, CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC="2025-10-09T08:53:20Z")
        self.assertEqual(NOW, self.wk._utc_deadline_env(at, "CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC"))
        self.assertEqual(1, len(self.problems(at, NOW)))
        self.assertEqual([], self.problems(at, NOW - 1))


class PilotTargetGateTest(unittest.TestCase):
    """The target check in `pilot on` against the reader it stands in front of (#386): what the
    script passes, phone_intake.target_from_env must pass, and the one line the script sends must
    read back as the same request. The script may refuse more; it may never pass more."""

    @classmethod
    def setUpClass(cls):
        cls.canonical = staticmethod(gate_constant("canonical", "PILOT_TARGET_PY"))
        cls.wk = load("claude_api_worker_for_pilot", WORKER / "main.py")
        cls.pilot = cls.wk.pilot                                  # phone_intake, as the worker itself holds it

    def worker_reads(self, raw):
        try:
            return self.pilot.target_from_env({"PHONE_DOC_PILOT_TARGET": raw})
        except self.pilot.TargetUnavailable:
            return None

    def test_the_two_originals_and_the_five_fields_are_the_workers_own(self):
        self.assertEqual(self.pilot.DOC_IDS, gate_constant("DOC_IDS", "PILOT_TARGET_PY"))
        self.assertEqual(self.pilot.TARGET_FIELDS, gate_constant("FIELDS", "PILOT_TARGET_PY"))
        self.assertEqual(TARGET, self.worker_reads(json.dumps(TARGET)))      # the fixture is one the worker takes
        self.assertEqual((TARGET_LINE, ""), self.canonical(json.dumps(TARGET, indent=1)))
        self.assertEqual(TARGET, self.worker_reads(TARGET_LINE))
        # The one line holds nothing a shell or gcloud's ^@^ form could split on unseen.
        self.assertNotRegex(TARGET_LINE, r"[\s@^'\\]")

    def test_the_check_is_never_looser_than_the_worker_and_the_line_it_sends_is_the_same_request(self):
        other_doc = self.pilot.DOC_IDS[0]
        cells = [TARGET, dict(TARGET, source_doc_id=other_doc)]
        for name, values in (
                ("source_doc_id", ("", "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP", other_doc + " ", other_doc.lower(), other_doc + "\n")),
                ("source_revision", ("", "r", "a" * 128, "a" * 129, "a b", "a,b", "a@b", "a\n", "rev:1.2-3_4")),
                ("request_id", ("", "short", "REQ-0001", "R" * 128, "R" * 129, "REQ 0001", "REQ-0001\n", "req.1:2-3_4")),
                ("task_id", ("", "task-", "task-0123456789abcde", "task-0123456789abcdef0", "task-0123456789ABCDEF",
                             "TASK-0123456789abcdef", "task-0123456789abcdef\n")),
                ("recipient_id", ("", "claude", "claude-api ", "Claude-API", "claude-code-cli"))):
            cells += [dict(TARGET, **{name: value}) for value in values]
            cells += [dict(TARGET, **{name: wrong}) for wrong in (None, 7, True, ["x"], {"x": "y"})]
        cells += [{k: v for k, v in TARGET.items() if k != name} for name in TARGET]      # a field short
        cells.append(dict(TARGET, extra="x"))                                             # a field over
        raws = [json.dumps(cell) for cell in cells]
        raws += ["", " ", "null", "[]", "7", '"x"', "{", json.dumps([TARGET]), json.dumps(TARGET) + "x",
                 json.dumps(TARGET).replace('"claude-api"', '"claude\\u002dapi"'),        # an escape the reader resolves
                 json.dumps(TARGET)[:-1] + ',"task_id":"task-ffffffffffffffff"}',         # a key twice: the last one wins
                 "\n" + json.dumps(TARGET, indent=4) + "\n"]
        passed = refused = 0
        for raw in raws:
            line, why = self.canonical(raw)
            read = self.worker_reads(raw)
            if line is None:
                refused += 1
                self.assertTrue(why, raw)
                # Refused by the script and taken by the worker would be a good pilot turned away.
                self.assertIsNone(read, raw)
                continue
            passed += 1
            self.assertIsNotNone(read, raw)                           # never looser
            self.assertEqual(read, self.worker_reads(line), raw)      # the line sent is the request given
            self.assertNotRegex(line, r"[\s@^'\\]", raw)
        self.assertGreaterEqual(passed, 8)                            # the sweep is not vacuous either way
        self.assertGreater(refused, 40)

    def test_what_the_verbs_refuse_to_leave_behind_is_what_stops_the_worker(self):
        # The control for pilot on, lane on and pilot off: each state they refuse to make, or clear,
        # is one main() answers by stopping before it reads anything. Flags as the JOB would hold them.
        def stops(worker="off", lane="off", pilot="off", target=""):
            said = io.StringIO()
            env = {"CLAUDE_API_WORKER_ENABLED": worker, "CLAUDE_API_LANE_ENABLED": lane,
                   "PHONE_DOC_INTAKE_ENABLED": pilot, "PHONE_DOC_PILOT_TARGET": target}
            with mock.patch.dict(os.environ, env), redirect_stdout(said):
                code = self.wk.main()
            return code, json.loads(said.getvalue().splitlines()[0])["status"]

        self.assertEqual((1, "pilot_target_invalid"), stops(worker="on", pilot="on"))                 # the flag alone: f011d08
        self.assertEqual((1, "pilot_target_invalid"), stops(worker="on", pilot="on", target=TARGET_LINE.replace('"', "")))
        self.assertEqual((1, "pilot_flags_invalid"), stops(worker="on", pilot="on", lane="on", target=TARGET_LINE))
        self.assertEqual((1, "pilot_flags_invalid"), stops(lane="on", pilot="on", target=TARGET_LINE))
        self.assertEqual((1, "pilot_flags_invalid"), stops(lane="on", target=TARGET_LINE))
        self.assertEqual((1, "pilot_flags_invalid"), stops(worker="on", target=TARGET_LINE))         # the flag off, the target left
        # And what `pilot on` leaves before `worker on`, and what `pilot off` leaves: a job that does nothing.
        self.assertEqual((0, "disabled"), stops(pilot="on", target=TARGET_LINE))
        self.assertEqual((0, "disabled"), stops())
        # Cursor on b975708: what `lane on` let through when it judged the first line only.
        for left_behind in ("\n", " ", "\n" + TARGET_LINE):
            self.assertEqual((1, "pilot_flags_invalid"), stops(lane="on", target=left_behind), repr(left_behind))
        # And what the read-back in `pilot on` took for the target it had sent.
        self.assertEqual((1, "pilot_target_invalid"), stops(worker="on", pilot="on", target=TARGET_LINE + "\nGARBAGE"))


class ReaderTest(unittest.TestCase):
    """ENV_LINES_PY by itself, no bash: the reader every read-back verb in both scripts stands on.
    It must find the variables wherever the answer keeps them, write one line for one variable and
    never the same line for two values, and say "unreadable" for what a line cannot speak for."""

    reader = quoted((WORKER / "deploy.sh").read_text(encoding="utf-8"), "ENV_LINES_PY")

    def read(self, answer):
        text = answer if isinstance(answer, str) else json.dumps(answer)
        done = subprocess.run([sys.executable, "-c", self.reader], input=text.encode("utf-8"), capture_output=True)
        return done.returncode, done.stdout.decode("ascii").replace("\r\n", "\n")   # ascii: the lines are nothing else

    @staticmethod
    def unescape(line):
        return re.sub(r"\\u([0-9a-f]{4})|\\U([0-9a-f]{8})", lambda m: chr(int(m.group(1) or m.group(2), 16)), line)

    def test_the_variables_are_found_in_every_shape_and_a_shape_it_does_not_know_is_unreadable(self):
        values = dict(CLAUDE_API_WORKER_ENABLED="off", PHONE_DOC_INTAKE_ENABLED="on", PHONE_DOC_PILOT_TARGET=TARGET_LINE)
        wanted = "CLAUDE_API_WORKER_ENABLED=off\nPHONE_DOC_INTAKE_ENABLED=on\nPHONE_DOC_PILOT_TARGET=%s\n" % TARGET_LINE
        container = {"env": env_list(values)}
        for name, answer in (("the v1 job, as gcloud 584 answers", job_json(values)),
                             ("the v2 job", job_json(values, shape="v2")),
                             ("the v1 service", service_json(values)),
                             ("the v2 service", {"template": {"containers": [container]}})):
            self.assertEqual((0, wanted), self.read(answer), name)
        # The secret in the job is not a line, and the target prints as it was sent, to the character.
        self.assertNotIn("ANTHROPIC_API_KEY", self.read(job_json(values))[1])
        for name, answer in (("containers somewhere else", {"spec": {"somewhere": {"containers": [container]}}}),
                             ("no containers", {"spec": {"template": {"spec": {"template": {"spec": {}}}}}}),
                             ("an empty list of them", {"template": {"template": {"containers": []}}}),
                             ("two containers", {"template": {"template": {"containers": [container, container]}}}),
                             ("a list", [container]), ("a string", '"x"'), ("not JSON", "Updates are available")):
            code, said = self.read(answer)
            self.assertEqual((3, ""), (code, said), name)             # unreadable, and nothing printed that could read as off

    def test_one_variable_is_one_line_and_no_two_values_are_the_same_line(self):
        values = ["", "on", "off", "on\r", "on\n", "on\nEXTRA", "\n", "\r", "\r\n", " ", "\t", "on ", " on", "\\", "\\n",
                  "\\u000a", "\\u005cu000a", "a=b", "x\nPHONE_DOC_INTAKE_ENABLED=on", TARGET_LINE, TARGET_LINE + "\nGARBAGE",
                  "\n" + TARGET_LINE, "café", " ", "\x00", "\x7f", "\x85", "\U0001f600", "ὠ" + "0"]
        self.assertEqual(len(values), len(set(values)))
        answer = job_json({"V%02d" % i: value for i, value in enumerate(values)})
        code, said = self.read(answer)
        self.assertEqual(0, code)
        lines = said.split("\n")[:-1]
        self.assertEqual(len(values), len(lines), lines)                              # one each, never two
        self.assertEqual(len(values), len({line.split("=", 1)[1] for line in lines}))  # never the same line twice
        for i, (value, line) in enumerate(zip(values, lines)):
            name, written = line.split("=", 1)
            self.assertEqual("V%02d" % i, name)
            self.assertRegex(written, r"\A[ -~]*\Z", repr(value))                     # printable ASCII and nothing else
            self.assertEqual(value, self.unescape(written), repr(value))             # and the value comes back whole
        # What is plain stays plain: a caller matches the value itself.
        for plain in ("on", "off", "a=b", " on", "on ", TARGET_LINE):
            self.assertIn("=%s\n" % plain, said)

    def test_a_flag_from_a_secret_or_a_name_twice_is_unreadable_and_an_empty_literal_is_not(self):
        source = {"secretKeyRef": {"key": "latest", "name": "x"}}
        for name, extra in (("a flag from a secret, v1", [{"name": "PHONE_DOC_INTAKE_ENABLED", "valueFrom": source}]),
                            ("a flag from a secret, v2", [{"name": "DRIVE_INTAKE_ENABLED", "valueSource": source}]),
                            ("the target from a secret", [{"name": "PHONE_DOC_PILOT_TARGET", "valueFrom": source}]),
                            ("a name twice", [{"name": "A", "value": "off"}, {"name": "A", "value": "on"}]),
                            ("a name twice, once from a secret", [{"name": "ANTHROPIC_API_KEY", "value": "x"}])):
            self.assertEqual((3, ""), self.read(job_json({}, extra)), name)
        # Any other secret is simply not a line, and an empty string is a value, written or left out.
        self.assertEqual((0, "A=\nB=\nC=x\n"), self.read(job_json({}, [{"name": "A"}, {"name": "B", "value": ""},
                                                                         {"name": "C", "value": "x"},
                                                                         {"name": "BUS_SECRET", "valueFrom": source}])))

    def test_both_scripts_carry_this_reader(self):
        self.assertEqual(self.reader, quoted((INTAKE / "deploy.sh").read_text(encoding="utf-8"), "ENV_LINES_PY"))


if __name__ == "__main__":
    unittest.main()
