"""The broken-telephone deploy scripts: what they say must match what the code reads, and what they
must never say (a public service, all-traffic egress, a flag on by default, a secret value).

A deploy script drifts from its code silently: a renamed env var in main.py and the job keeps
running with the default. So every `environ.get("NAME")` the two services read must be set by the
script that deploys them, or be on the short list of optional ones, and nothing else.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INTAKE = REPO / "cloud" / "drive-intake"
WORKER = REPO / "cloud" / "claude-api-worker"
# environ.get("NAME") and the two helpers that take the name as a string. A helper call is still a
# read: _int_env(environ, "CAP") is how the cap, the reservation, the heartbeat and the attempt
# limit are read, and a regex that only sees environ.get misses every one of them.
ENV_READ = re.compile(
    r'(?:environ\.get\(\s*"([A-Z0-9_]+)"|(?:_int_env|_float_env)\(\s*environ\s*,\s*"([A-Z0-9_]+)")'
)

# Read by the code, deliberately not set by the deploy: optional prices (the code treats unset as
# "no price"), the wake ring until #359 lands, and Cloud Run's own PORT. Max tokens is set.
OPTIONAL = {
    # DRIVE_INTAKE_CONFIRM_TOKEN: the task filter's G5 token. Unset is the safe state (no CONFIRM ever
    # matches, so every dial or spend is dry_run); wiring it as a secret is a later owner decision.
    # DRIVE_INTAKE_STUDIO_URL and DRIVE_INTAKE_PAIR_TTL: the studio link (the telephone binding). Unset is
    # the safe state (no link is written and /pair answers enabled=false); turning it on is an owner decision.
    "drive-intake": {"INBOX_WAKE_URL", "INBOX_WAKE_SECRET", "PORT", "DRIVE_INTAKE_BIND",
                     "DRIVE_INTAKE_CONFIRM_TOKEN", "DRIVE_INTAKE_STUDIO_URL", "DRIVE_INTAKE_PAIR_TTL"},
    "claude-api-worker": {"CLAUDE_API_WORKER_USD_PER_MTOK_IN", "CLAUDE_API_WORKER_USD_PER_MTOK_OUT",
                          "FLEET_CAD_PER_USD"},
}
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


if __name__ == "__main__":
    unittest.main()
