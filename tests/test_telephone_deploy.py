"""The broken-telephone deploy scripts: what they say must match what the code reads, and what they
must never say (a public service, all-traffic egress, a flag on by default, a secret value).

A deploy script drifts from its code silently: a renamed env var in main.py and the job keeps
running with the default. So every `environ.get("NAME")` the two services read must be set by the
script that deploys them, or be on the short list of optional ones, and nothing else.
"""
import os
import re
import shutil
import subprocess
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
        self.assertLess(trail.index('grep -Eq "$DOC_ID_OK"'), trail.index("service_url"))
        self.assertLess(trail.index('grep -Eq "$DOC_ID_OK"'), trail.index("secrets versions access"))
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
        self.assertIn('[ "$1" = on ] && env_lines jobs "$WORKER_JOB"', flag)
        self.assertIn("exit 2", flag)
        env_lines = function_body(self.script, "env_lines")
        self.assertIn("--format json", env_lines)
        self.assertIn('"$PYTHON" -c "$ENV_LINES_PY"', env_lines)


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
        self.assertIn('env_lines jobs "$JOB" | "$PYTHON" -c "$COST_MISSING_PY"', function_body(self.script, "cost_missing"))
        for name in COST_INPUTS + ("CLAUDE_API_WORKER_MODEL",):
            self.assertIn('"%s"' % name, self.script.split("COST_MISSING_PY=", 1)[1].split("\n'\n", 1)[0], name)
        self.assertIn("return float(value) == 0", self.script)                   # "0" and "0.0" are unset; a date is set
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
  *"run jobs describe"*"--format json"*) printf '%s' "$FAKE_JOB_JSON" ;;
  *"run services describe"*"--format json"*) printf '%s' "$FAKE_SERVICE_JSON" ;;
  *"secrets versions access"*) printf '%s' "tok-123" ;;
  *"auth print-identity-token"*) printf '%s' "idt-456" ;;
esac
'''
FAKE_CURL = r'''#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$FAKE_LOG"
printf '%s' '{"status":"ok","enabled":true}'
'''
JOB_JSON = '{"template":{"template":{"containers":[{"env":[%s]}]}}}'                 # the v2 job shape
SERVICE_JSON = '{"spec":{"template":{"spec":{"containers":[{"env":[%s]}]}}}}'        # the Knative service shape
DOC = "1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP"
SET = dict(CLAUDE_API_WORKER_MODEL="m", CLAUDE_API_WORKER_USD_PER_MTOK_IN="0.1", CLAUDE_API_WORKER_USD_PER_MTOK_OUT="0.5",
           FLEET_CAD_PER_USD="1.45", CLAUDE_API_WORKER_MAX_INPUT_TOKENS="100000",
           CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC="2026-10-15T04:00:00Z")
DEFAULTS = dict(CLAUDE_API_WORKER_MODEL="m", CLAUDE_API_WORKER_USD_PER_MTOK_IN="0", CLAUDE_API_WORKER_USD_PER_MTOK_OUT="0",
                FLEET_CAD_PER_USD="0", CLAUDE_API_WORKER_MAX_INPUT_TOKENS="0", CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC="")


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
        for name, text in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
            (cls.tmp / name).write_text(text, encoding="utf-8", newline="\n")
            os.chmod(cls.tmp / name, 0o755)
        cls.calls = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def verb(self, script, *args, job=None, service=None, studio_sa="studio-sa@sfdc24.iam.gserviceaccount.com"):
        type(self).calls += 1
        log = self.tmp / ("calls-%d.log" % self.calls)
        env = dict(os.environ, GCLOUD=(self.tmp / "gcloud").as_posix(),
                   PATH=str(self.tmp) + os.pathsep + os.environ.get("PATH", ""),
                   FAKE_LOG=log.as_posix(), FAKE_STUDIO_SA=studio_sa,
                   FAKE_JOB_JSON=JOB_JSON % env_items(**(job or {})),
                   FAKE_SERVICE_JSON=SERVICE_JSON % env_items(**(service or {})))
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
        self.assertIn("unset or zero on claude-api-worker:", run.stdout)
        missing = run.stdout.split("unset or zero on claude-api-worker:", 1)[1]
        self.assertIn("CLAUDE_API_WORKER_USD_PER_MTOK_OUT", missing)
        self.assertIn("CLAUDE_API_WORKER_AUTHORIZED_UNTIL_UTC", missing)
        self.assertNotIn("CLAUDE_API_WORKER_USD_PER_MTOK_IN", missing)
        run, _ = self.verb(WORKER / "deploy.sh", "cost", job=SET)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn("every cost input is set", run.stdout)

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


if __name__ == "__main__":
    unittest.main()
