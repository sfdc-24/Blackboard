"""The broken-telephone deploy scripts: what they say must match what the code reads, and what they
must never say (a public service, all-traffic egress, a flag on by default, a secret value).

A deploy script drifts from its code silently: a renamed env var in main.py and the job keeps
running with the default. So every `environ.get("NAME")` the two services read must be set by the
script that deploys them, or be on the short list of optional ones, and nothing else.
"""
import importlib.util
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INTAKE = REPO / "cloud" / "drive-intake"
WORKER = REPO / "cloud" / "claude-api-worker"
# environ.get("NAME") and the two helpers that take the name as a string. A helper call is still a
# read: _int_env(environ, "CAP") is how the cap, the reservation, the heartbeat and the attempt
# limit are read, and a regex that only sees environ.get misses every one of them.
ENV_READ = re.compile(
    r'(?:environ\.get\(\s*"([A-Z0-9_]+)"|(?:_int_env|_float_env|_utc_deadline_env)\(\s*environ\s*,\s*"([A-Z0-9_]+)")'
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
        self.assertIn("curl -sS --fail-with-body", trail)         # an HTTP refusal is not a proof
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
        read = env_names_read(WORKER / "main.py", WORKER / "lane.py")
        read -= {"CLAUDE_API_WORKER_ENABLED", "CLAUDE_API_LANE_ENABLED"}
        missing = read - env_names_set(self.script) - OPTIONAL["claude-api-worker"] - VIA_SECRET_OR_MOUNT
        self.assertEqual(set(), missing)
        self.assertIn("CLAUDE_API_WORKER_ENABLED=off", self.script)
        self.assertIn("CLAUDE_API_LANE_ENABLED=off", self.script)
        self.assertIn("PHONE_DOC_INTAKE_ENABLED=off", self.script)
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
        self.assertNotRegex(self.script, r"--format '[^']*spec\.template")
        self.assertIn("roles/iam.serviceAccountTokenCreator", self.script)
        self.assertNotIn("roles/iam.serviceAccountUser", self.script)
        self.assertIn("template.template.serviceAccount", self.script)

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
        self.assertIn("pilot) case \"${2:-}\" in on) pilot_on ;; off) flag PHONE_DOC_INTAKE_ENABLED off ;;", self.script)
        pilot_on = function_body(self.script, "pilot_on")
        self.assertIn('INTAKE_SERVICE="${INTAKE_SERVICE:-drive-intake}"', self.script)
        self.assertLess(pilot_on.index('grep -qx "DRIVE_INTAKE_ENABLED=on"'), pilot_on.index("flag PHONE_DOC_INTAKE_ENABLED on"))
        self.assertIn('env_lines services "$INTAKE_SERVICE"', pilot_on)
        self.assertIn("exit 2", pilot_on)
        self.assertIn("pilot on|off", self.script)


class ShellTest(unittest.TestCase):
    def test_both_scripts_parse_in_bash_and_show_usage_without_a_verb(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash on this box")
        for script in (INTAKE / "deploy.sh", WORKER / "deploy.sh"):
            self.assertEqual(0, subprocess.run([bash, "-n", str(script)], capture_output=True).returncode, script)
            usage = subprocess.run([bash, str(script)], capture_output=True, text=True, cwd=str(REPO))
            self.assertEqual(2, usage.returncode, script)
            self.assertIn("deploy.sh setup", usage.stdout, script)
            self.assertNotIn("gcloud", usage.stderr, script)    # usage never calls gcloud
            # The usage is the header, printed by line range: a verb added to the header must still print.
            last_verb = "deploy.sh trail <doc id>" if script.parent == INTAKE else "pilot on|off"
            self.assertIn(last_verb, usage.stdout, script)
            self.assertNotIn("set -euo pipefail", usage.stdout, script)

    def test_a_bad_doc_id_never_reaches_gcloud_and_a_good_one_does(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash on this box")
        # gcloud is a path that does not exist: a call reaching it fails loudly, by name.
        env = dict(os.environ, GCLOUD="/nonexistent/gcloud")
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
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$*" in
  *"value(status.url)"*) echo "https://drive-intake-fake-uc.a.run.app" ;;
  *"serviceAccountName"*) printf '%s\n' "$FAKE_STUDIO_SA" ;;
  *"run jobs describe"*"--format json"*) describe "${FAKE_JOB_FAILS:-}" "$FAKE_JOB_JSON" job ;;
  *"run services describe"*"--format json"*) describe "${FAKE_SERVICE_FAILS:-}" "$FAKE_SERVICE_JSON" service ;;
  *"secrets versions access"*) printf '%s' "tok-123" ;;
  *"auth print-identity-token"*) printf '%s' "idt-456" ;;
esac
'''
# How a describe goes wrong, in gcloud's own words: absent, denied, down, or an answer that is not JSON.
FAKE_DESCRIBE = r'''describe() {
  case "$1" in
    notfound) echo "ERROR: (gcloud.run.describe) Cannot find $3 [x]" >&2; exit 1 ;;
    denied) echo "ERROR: (gcloud.run.describe) PERMISSION_DENIED: Permission denied on resource x (or it may not exist). NOT_FOUND is not what this says" >&2; exit 1 ;;
    down) echo "ERROR: (gcloud.run.describe) UNAVAILABLE: The service is currently unavailable." >&2; exit 1 ;;
    garbage) printf '%s' "Updates are available for some Google Cloud CLI components." ;;
    list) printf '%s' "[]" ;;
    *) printf '%s' "$2" ;;
  esac
}
'''
FAKE_GCLOUD = FAKE_GCLOUD.replace('printf', FAKE_DESCRIBE + 'printf', 1)
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
JOB_JSON = '{"template":{"template":{"containers":[{"env":[%s]}]}}}'                 # the v2 job shape
SERVICE_JSON = '{"spec":{"template":{"spec":{"containers":[{"env":[%s]}]}}}}'        # the Knative service shape
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
    ("CLAUDE_API_WORKER_MAX_INPUT_TOKENS", "100k"),
    ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2099-01-01T00:00:00+00:00"),
    ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2099-01-01T00:00:00z"),
    ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2099-01-01"),
    ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2099-13-01T00:00:00Z"),
    ("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", "2020-01-01T00:00:00Z"),          # well-formed, and past
    ("FLEET_SPEND_CAP_CENTS_CAD", "0"), ("FLEET_SPEND_CAP_CENTS_CAD", "25.00"),
    ("CLAUDE_API_WORKER_RESERVE_CENTS_CAD", "0"), ("CLAUDE_API_WORKER_MAX_TOKENS", "0"),
    ("CLAUDE_API_WORKER_MODEL", "  "),
)


def env_items(**values):
    return ",".join('{"name":"%s","value":"%s"}' % item for item in values.items())


class FakeGcloudTest(unittest.TestCase):
    """The verbs that read a deployed resource back, driven end to end against a fake gcloud (and a
    fake curl) that log every call: what each verb refuses, what it then runs, what it never prints.
    The string tests above pin the text; this is the path."""

    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if not cls.bash:
            raise unittest.SkipTest("no bash on this box")
        cls.tmp = Path(tempfile.mkdtemp(prefix="fake-gcloud-"))
        for name, text in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL), ("crlf-python", FAKE_CRLF_PYTHON)):
            (cls.tmp / name).write_text(text, encoding="utf-8", newline="\n")
            os.chmod(cls.tmp / name, 0o755)
        cls.calls = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def verb(self, script, *args, job=None, service=None, studio_sa="studio-sa@sfdc24.iam.gserviceaccount.com",
             **fakes):
        type(self).calls += 1
        log = self.tmp / ("calls-%d.log" % self.calls)
        env = dict(os.environ, GCLOUD=(self.tmp / "gcloud").as_posix(),
                   PATH=str(self.tmp) + os.pathsep + os.environ.get("PATH", ""),
                   FAKE_LOG=log.as_posix(), FAKE_STUDIO_SA=studio_sa,
                   FAKE_JOB_JSON=JOB_JSON % env_items(**(job or {})),
                   FAKE_SERVICE_JSON=SERVICE_JSON % env_items(**(service or {})), **fakes)
        run = subprocess.run([self.bash, str(script), *args], capture_output=True, text=True, cwd=str(REPO), env=env)
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return run, calls

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
        run, calls = self.verb(WORKER / "deploy.sh", "pilot", "on", service=dict(DRIVE_INTAKE_ENABLED="on"))
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertIn("one intake at a time", run.stderr)
        self.assertEqual([], [c for c in calls if "run jobs update" in c])
        run, calls = self.verb(WORKER / "deploy.sh", "pilot", "on", service=dict(DRIVE_INTAKE_ENABLED="off"))
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(1, len([c for c in calls if c.startswith("run jobs update claude-api-worker")
                                 and "PHONE_DOC_INTAKE_ENABLED=on" in c]), calls)
        run, calls = self.verb(WORKER / "deploy.sh", "pilot", "on", service={})           # no service: nothing to collide with
        self.assertEqual(0, run.returncode, run.stderr)

    def test_studio_grants_the_discovered_account_and_prints_the_url_never_the_token(self):
        run, calls = self.verb(INTAKE / "deploy.sh", "studio")
        self.assertEqual(0, run.returncode, run.stderr)
        member = "--member serviceAccount:studio-sa@sfdc24.iam.gserviceaccount.com"
        self.assertEqual(1, len([c for c in calls if c.startswith("run services add-iam-policy-binding drive-intake")
                                 and member in c and "--role roles/run.invoker" in c]), calls)
        self.assertEqual(1, len([c for c in calls if c.startswith("secrets add-iam-policy-binding DRIVE_INTAKE_POLL_TOKEN")
                                 and member in c and "secretmanager.secretAccessor" in c]), calls)
        self.assertIn("--update-env-vars STUDIO_TELEPHONE_URL=https://drive-intake-fake-uc.a.run.app "
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
        self.assertIn("https://drive-intake-fake-uc.a.run.app/trail?doc=" + DOC, curls[0])
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
        for how in ("denied", "down", "garbage", "list"):
            run, calls = self.verb(INTAKE / "deploy.sh", "on", job=dict(PHONE_DOC_INTAKE_ENABLED="on"), FAKE_JOB_FAILS=how)
            self.assertEqual(2, run.returncode, how + run.stderr)
            self.assertIn("could not be read back", run.stderr, how)
            self.assertEqual([], [c for c in calls if "run services update" in c], how)
            run, calls = self.verb(WORKER / "deploy.sh", "pilot", "on", service=dict(DRIVE_INTAKE_ENABLED="on"),
                                   FAKE_SERVICE_FAILS=how)
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
        run, calls = self.verb(WORKER / "deploy.sh", "pilot", "on", FAKE_SERVICE_FAILS="notfound")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(1, len([c for c in calls if "run jobs update" in c and "PHONE_DOC_INTAKE_ENABLED=on" in c]), calls)
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
        run, calls = self.verb(WORKER / "deploy.sh", "pilot", "on", service=dict(DRIVE_INTAKE_ENABLED="on"), **crlf)
        self.assertEqual(2, run.returncode, run.stderr)
        self.assertEqual([], [c for c in calls if "run jobs update" in c])
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

    def runtime_can_call(self, env, now):
        """main.py's verdict on this env, by its own readers and the conditions call_reservation states."""
        wk = self.wk
        until = wk._utc_deadline_env(env, "CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC")
        return bool(
            (env.get("CLAUDE_API_WORKER_MODEL") or "").strip()
            and wk._int_env(env, "CLAUDE_API_WORKER_RESERVE_CENTS_CAD", 0) > 0
            and wk._int_env(env, "FLEET_SPEND_CAP_CENTS_CAD", 0) > 0
            and wk._int_env(env, "CLAUDE_API_WORKER_MAX_TOKENS", wk.DEFAULT_MAX_TOKENS) > 0
            and wk._int_env(env, "CLAUDE_API_WORKER_MAX_INPUT_TOKENS", 0) > 0
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
        ints = ("100000", " 100000 ", "+7", "1_000", "07", "1e5", "100000.0", "", "0", "-1", "0x10")
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


if __name__ == "__main__":
    unittest.main()
