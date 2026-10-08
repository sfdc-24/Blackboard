"""The Memorystore dual-run: a second store that is shadowed, never trusted, and off by default.

HIS SHAPE, IN HIS WORDS (2026-10-05 GO): "non-disruptive Redis dual-run for Blackboard / related
workflows - old path stays authoritative; add a second Memorystore connection; background read from
Redis first; write-through to both; off-switch via settings not redeploy."

WHAT "NON-DISRUPTIVE" IS BUILT TO MEAN HERE, because it is easy to say and easy to lose:

  1. The old path answers. Every read returns what the authoritative source returned, even when Redis
     disagrees, even when Redis is faster, even when Redis is right. A divergence is a LOG LINE, never
     a different answer. A cache you start trusting on day one is a cache whose first wrong answer you
     discover in front of a client.
  2. Nothing is attempted while it is off, and OFF IS THE DEFAULT. Disabled means no import of the
     client library, no socket, no DNS, no secret read. A feature that connects in order to discover
     it is disabled is not off.
  3. The off-switch is a settings FILE, re-read on every call, so turning it off is one edit and
     takes effect on the next operation IN WHATEVER PROCESS READS THAT FILE. No restart, no
     process to find and kill.
     BOUNDED, because the unbounded version of this sentence was wrong and shipped: a deployed
     Cloud Run job reads the copy BAKED INTO ITS IMAGE, so editing the repository file changes
     nothing there without a rebuild. Mount a copy and point REDIS_DUAL_SETTINGS at it to make the
     file operable on a job, or use the job's environment (REDIS_CONNECT=false) for the next
     execution. Neither stops an execution already connected. See Settings and
     cloud/bus-reconciler/REQUESTS.md.
  4. A mirror failure is never the caller's problem. The write to Redis happens after the
     authoritative write has succeeded, and anything it raises is swallowed into a counter.

WHAT CANNOT BE A DUAL-RUN TARGET, measured rather than assumed: the Blackboard bus itself. The board
is a Google Sheet behind Apps Script, and Apps Script has no VPC access of any kind, so it can never
reach a private Memorystore address. Only code that runs in Cloud Run, on a VM, or on a machine
inside the authorised VPC can take part. The host is a PRIVATE service-access address: this laptop
and Cloud Shell cannot reach it either, which is why `enabled` being off is also the honest state of
the world until egress exists.

THE SECRET. The AUTH string lives in Secret Manager (project sfdc24, secret REDIS_AUTH_STRING) and is
read at connect time. It is never written to git, never to the board, never to a log, and never
returned by anything here. `status()` reports whether an auth string was FOUND, not what it is - the
rule from our secret checks is that a check prints the type and not the value.

WHERE THE ADDRESS AND THE CERT LIVE, and why not here. This repository is PUBLIC. The AUTH string is
in Secret Manager and never leaves it. The instance's private address and its server CA are not
credentials, but they are topology, and topology beside everything else this repo says about our fleet
is needless disclosure - so the committed settings file is a TEMPLATE with an empty host, and the real
values arrive as REDIS_HOST and REDIS_CA_CERT_PATH in the environment. Process environment wins over
the file, the same contract as bus.load_env.

    python scripts/redis_dual.py status        # what is configured, what is reachable, nothing secret
    python scripts/redis_dual.py selftest      # exercise the whole wrapper against a fake client
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _settings_path():
    """Where the switches live, in a repository checkout AND in the flat Cloud Run image.

    In the repo this module sits in scripts/ beside its settings file. In the image every file is
    copied individually into /app, so parents[1] is "/" and the repo-shaped path pointed at
    /scripts/redis_dual.settings.json, which cannot exist. Under the old code a missing file meant
    "{}" and the environment could still turn the dual-run on - so the jobs connected and nobody
    noticed the file had never travelled at all. Once an unreadable file correctly VETOES both
    switches, that absence became a hard stop, which is exactly how it surfaced: the first run on
    the fixed image reported settings_file_readable=false and refused to connect.

    Fail-closed was right and found a real gap. The order is written out so the next layout does not
    have to rediscover it: the environment, then the repository shape, then beside this module."""
    named = os.environ.get("REDIS_DUAL_SETTINGS")
    if named:
        return Path(named)
    in_repo = REPO / "scripts" / "redis_dual.settings.json"
    if in_repo.is_file():
        return in_repo
    return Path(__file__).resolve().parent / "redis_dual.settings.json"


SETTINGS_PATH = _settings_path()
# Memorystore's own default is 6379; his instance answers on 6378 with TLS required.
DEFAULT_PORT = 6378
# EVERY KEY THIS FLEET WRITES CARRIES A VERSION. Gemini, architect lead, 2026-10-06 15:21:43Z:
# "Implement the v1 colon prefix immediately... schemas always change. Without a version namespace,
# future migrations break your dual-run philosophy and force either downtime or key collisions. Since
# the keyspace is currently empty, fixing this architectural gap now costs nothing."
#
# I had applied the dual-run philosophy - old path authoritative, run both, compare - to every store
# in this fleet EXCEPT the keyspace itself, and only noticed while listing decisions for review. Three
# bytes a key against the ability to run v1 and v2 side by side is not a trade worth thinking about.
KEY_VERSION = "v1:"
SECRET_NAME = "REDIS_AUTH_STRING"
# Where Cloud Run mounts it. A file, not an environment variable: an env var is listable from
# anything that can read the process.
AUTH_FILE_ENV = "REDIS_AUTH_FILE"
# WHERE CLOUD RUN MOUNTS THEM, defaulted HERE and in no entrypoint. I wrote "one source of truth for
# where a secret lands, instead of two that can disagree" in a commit message and then put the
# defaults in TWO entrypoints - and the third entrypoint, roster_seed.py, forgot them and reported
# "no Redis connection" on a path that works. The fix is not a third copy; it is that no entrypoint
# needs to know. Harmless off Cloud Run, where neither file exists.
MOUNTED_CA = "/secrets/ca/redis-ca.pem"
MOUNTED_AUTH = "/secrets/auth/redis-auth"
PROJECT = "sfdc24"
# A reachability probe that cannot hang a caller. The host is private, so off the VPC this fails fast
# and the dual-run stays off rather than blocking a board read behind a TCP timeout.
PROBE_SECONDS = float(os.environ.get("REDIS_PROBE_SECONDS", "1.5"))
# The real connect gets longer than the pre-check. The pre-check exists to fail fast on a hot
# path; a connection that is actually wanted should not inherit a hot path's impatience.
CONNECT_SECONDS = float(os.environ.get("REDIS_CONNECT_SECONDS", "10"))


class Settings:
    """The dual-run's switches, re-read from disk on every call.

    Re-read on purpose. A settings object cached at import time is a switch you cannot flip without
    a redeploy, which is exactly what he said not to build.

    BUT RE-READING IS NOT THE SAME AS BEING OPERABLE, and I claimed the stronger thing. Codex,
    reviewing 314bead: the Dockerfile bakes this file into the image, so for a deployed Cloud Run
    job "edit the file, no redeploy" was false - the copy being re-read is the one inside the
    image. The veto itself held; the operational promise did not.

    What is actually true, stated so nobody relies on the wrong one:
      a checkout        editing the file takes effect on the next call. No restart.
      a Cloud Run job   the image's copy is read. Point REDIS_DUAL_SETTINGS at a mounted file to
                        make it operable without a rebuild; otherwise the switch is the job's
                        environment (REDIS_CONNECT=false), which applies to the NEXT execution.
      either            nothing here stops an execution that is already connected. The bound on
                        that is the task timeout and the probe's own 10-second TTL, not this file.
    """

    # TWO SWITCHES, BECAUSE ONE WAS DOING TWO JOBS.
    #
    # Copilot, PR 323: with REDIS_DUAL_ENABLED=true in the environment, a file saying
    # enabled=false - and equally a malformed file, or no file at all - still left live() true. The
    # environment merge defeated the file off-switch for both reads and writes. Correct, and worse
    # than it reads: the deployed bus-requests job carries exactly that variable, so "set
    # enabled=false and it stops immediately, no redeploy" was false for every job actually running.
    #
    # Fixing it exposed the real defect. `enabled` was answering two different questions:
    #     "is the DUAL-RUN on?"       - shadow reads and write-through beside a production path
    #     "may this process CONNECT?" - a deliberate probe, reconciler or request run
    # The diagnostic jobs only ever wanted the second, so they asserted the first to get it, and
    # that is precisely why turning the dual-run off could not turn them off either. They are two
    # settings now: `enabled` governs the dual-run, `connect` governs opening a socket at all.
    DEFAULTS = {
        "enabled": False,              # the off-switch. OFF IS THE DEFAULT and stays off until he says.
        "connect": False,              # may a deliberate job open a socket? Also off by default.
        "host": "",                    # empty means "nowhere to go": nothing is attempted
        "port": DEFAULT_PORT,
        "tls": True,                   # SERVER_AUTHENTICATION on his instance; never downgrade silently
        "ca_cert_path": "",            # the instance CA; TLS verification needs it
        "shadow_reads": True,          # read Redis in the BACKGROUND and compare; never serve from it
        "write_through": True,         # mirror a write after the authoritative write succeeds
        "key_prefix": KEY_VERSION + "blackboard:",
        "note": "Set enabled=false to stop the dual-run and connect=false to stop every connection. "
                "The FILE WINS: neither can be switched back on from the environment, and a missing "
                "or malformed file reads as both off. Re-read on every call - in whatever process "
                "reads THIS file; a deployed job reads the copy in its image.",
    }

    # Settings the environment may only turn DOWN, never up. Everything else - the address, the port,
    # the CA path - the environment supplies freely, because THIS REPOSITORY IS PUBLIC and the
    # instance's private address is not committed anywhere.
    FILE_WINS = ("enabled", "connect")

    FROM_ENV = {
        "REDIS_DUAL_ENABLED": ("enabled", lambda v: v.strip().lower() in ("1", "true", "yes", "on")),
        "REDIS_CONNECT": ("connect", lambda v: v.strip().lower() in ("1", "true", "yes", "on")),
        "REDIS_HOST": ("host", str.strip),
        "REDIS_PORT": ("port", lambda v: int(v.strip())),
        "REDIS_CA_CERT_PATH": ("ca_cert_path", str.strip),
    }

    def __init__(self, data=None, path=None, environ=None):
        self.path = Path(path) if path else SETTINGS_PATH
        self._data = dict(self.DEFAULTS)
        # Whether the file was READ, kept apart from what it said. A file that is missing or
        # unparseable is not a file that said nothing: it is a file that cannot authorise anything,
        # and it vetoes the environment exactly as an explicit false would.
        self.file_ok = data is not None
        if data is not None:
            self._data.update(data)
        else:
            on_disk = self._from_disk()
            self.file_ok = on_disk is not None
            self._data.update(on_disk or {})
        self._data.update(self._mounted())
        self._data.update(self._from_env(environ if environ is not None else os.environ))

    def _from_disk(self):
        """What the file says, or None when there is no readable file. None and {} are different:
        {} is a file that omitted a key, None is no authority at all."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None                # no settings file, or an unreadable one: authorises nothing
        return raw if isinstance(raw, dict) else None

    def _mounted(self) -> dict:
        """The Cloud Run mounts, used when nothing else named a path and the file is actually there.
        Existence is checked rather than assumed: a path that points at nothing is worse than none."""
        found = {}
        if os.path.exists(MOUNTED_CA):
            found["ca_cert_path"] = MOUNTED_CA
        return found

    def _from_env(self, environ) -> dict:
        """Environment overrides. A value this cannot parse is IGNORED, which leaves the setting at
        whatever the file said. A malformed override must never fail open into a live connection.

        For the two switches in FILE_WINS the environment can only agree with the file or turn it
        off - never on. That is the whole fix for the defeated off-switch: an operator editing the
        file stops the dual-run even while every deployed job still carries REDIS_DUAL_ENABLED=true,
        and no amount of environment can put it back."""
        found = {}
        for name, (key, cast) in self.FROM_ENV.items():
            raw = environ.get(name)
            if raw is None or raw == "":
                continue
            try:
                value = cast(raw)
            except (ValueError, TypeError):
                continue
            if key in self.FILE_WINS:
                # A switch is on only when BOTH the file and the environment say so, and only when
                # the file was readable at all. One veto is enough.
                value = bool(value) and bool(self._data.get(key)) and self.file_ok
            found[key] = value
        return found

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def _switch(self, name) -> bool:
        """One switch, with the file's veto applied. A file that could not be read vetoes both."""
        return self.file_ok and bool(self._data.get(name))

    def live(self) -> bool:
        """Whether the DUAL-RUN is on: shadow reads and write-through beside a production path."""
        return self._switch("enabled") and bool(str(self._data.get("host") or "").strip())

    def connect_ok(self) -> bool:
        """Whether a deliberate job - a probe, the reconciler, the request worker - may open a
        socket. Separate from live() on purpose: those jobs are measurements, not a dual-run, and
        conflating them is what let the dual-run's off-switch be bypassed by an env var."""
        return self._switch("connect") and bool(str(self._data.get("host") or "").strip())


def auth_string(project=PROJECT, secret=SECRET_NAME, runner=None, environ=None):
    """The AUTH string, or "" when it cannot be read. A FILE first, then gcloud.

    Returned to the caller that is about to connect and to nobody else. Never logged, never printed,
    never put in a return value that is reported.

    THE FILE PATH EXISTS BECAUSE THE CONTAINER HAS NO GCLOUD, and I found that while deploying rather
    than after. `python:3.12-slim` has no Cloud SDK in it, so the gcloud route below - which is the
    right one on a developer box - would have failed inside the job and reported "no auth string",
    which reads as a configuration mistake rather than as the missing binary it is. Cloud Run mounts
    a secret as a file, so REDIS_AUTH_FILE is how it arrives in production.

    A FILE rather than an environment variable, deliberately: an env var is listable from anything
    that can read the process, and a mounted file is not. The CA goes the same way for consistency,
    though a CA is not a secret.

    gcloud stays as the LOCAL fallback. It is also why this takes a runner: the only way to test the
    secret path without a secret."""
    environ = environ if environ is not None else os.environ
    path = (environ.get("REDIS_AUTH_FILE") or "").strip()
    if not path and os.path.exists(MOUNTED_AUTH):
        path = MOUNTED_AUTH          # the mount, without any entrypoint having to name it
    if path:
        try:
            found = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            found = ""
        if found:
            return found
        # An empty or unreadable mount is NOT a reason to fall through to gcloud: in production the
        # mount is the source, and quietly reaching for something else hides a broken deployment.
        return ""
    import subprocess
    gcloud = environ.get("GCLOUD", "gcloud")
    args = [gcloud, "secrets", "versions", "access", "latest",
            "--secret", secret, "--project", project]
    run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True,
                                              encoding="utf-8", timeout=30, shell=False))
    try:
        done = run(args)
    except Exception:
        return ""
    return (done.stdout or "").strip() if getattr(done, "returncode", 1) == 0 else ""


def reachable(host, port, seconds=PROBE_SECONDS, connector=None) -> bool:
    """Whether a TCP connection opens inside `seconds`. False off the VPC, which is the common case.

    This exists so a caller never waits on a private address it cannot reach. The dual-run being
    unreachable must cost a board read nothing."""
    if not host:
        return False
    probe = connector or socket.create_connection
    try:
        conn = probe((host, int(port)), seconds)
    except Exception:
        return False
    try:
        conn.close()
    except Exception:
        pass
    return True


def client(settings, factory=None, precheck=True):
    """A connected client, or None. None is an ordinary outcome, not an error.

    The redis library is NOT a dependency of this repository and is not installed on this box. That is
    deliberate: an import at module scope would make every script that touches the bus fail to start
    on a machine that will never run the dual-run. It is imported here, inside the one function that
    needs it, and its absence turns the dual-run off rather than breaking the caller.

    GATED ON connect_ok(), NOT live(). A probe or a reconciler run is a measurement, not a dual-run,
    and giving them the dual-run's switch to assert is what let an env var defeat its off-switch."""
    if not settings.connect_ok():
        return None
    if factory is not None:
        return factory(settings)
    try:
        import redis                                    # noqa: PLC0415 - see the docstring
    except ImportError:
        return None
    # THE PRE-CHECK IS A LATENCY GUARD, NOT A REACHABILITY VERDICT, and conflating the two produced a
    # false negative the first time it mattered: a 1.5-second TCP probe from a cold gen2 container
    # whose VPC interface was still coming up said "unreachable" and the connectivity probe reported
    # FAIL on a path that worked seconds earlier from an identically configured job.
    #
    # On a hot path - a board read that must not hang behind a private address - a fast "no" is worth
    # more than a correct "yes", so the guard stays there. A job whose whole purpose is to connect
    # should not be gated by it: it passes precheck=False and lets the real connect, with its own
    # timeout, be the authority. The answer from an attempt beats the answer from a guess.
    if precheck and not reachable(settings.host, settings.port):
        return None
    secret = auth_string()
    if not secret:
        return None                                     # AUTH is required on his instance
    kwargs = {"host": settings.host, "port": int(settings.port), "password": secret,
              "socket_timeout": CONNECT_SECONDS, "socket_connect_timeout": CONNECT_SECONDS,
              "decode_responses": True}
    if settings.tls:
        kwargs.update({"ssl": True, "ssl_cert_reqs": "required"})
        if settings.ca_cert_path:
            kwargs["ssl_ca_certs"] = settings.ca_cert_path
    try:
        return redis.Redis(**kwargs)
    except Exception:
        return None


class Counters:
    """What the dual-run did, for the VERIFY he asked for. Counts only: no keys, no values, no secret."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"shadow_reads": 0, "shadow_hits": 0, "shadow_misses": 0, "shadow_errors": 0,
                     "agreed": 0, "diverged": 0, "mirrors": 0, "mirror_errors": 0, "skipped_off": 0}

    def bump(self, name, by=1):
        with self.lock:
            self.data[name] = self.data.get(name, 0) + by

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.data)


class DualRun:
    """The authoritative path, with Redis shadowed beside it.

    `log` takes one string. `clock` is injectable so a test does not sleep."""

    # The most shadow reads allowed in flight at once. Copilot, PR 323: every enabled read appended
    # a worker and only drain() ever removed one, so 20 reads left 20 dead Thread objects and slow
    # reads left unbounded LIVE ones. A diagnostic that can exhaust the process it is observing is
    # worse than no diagnostic - so admission is bounded and NON-BLOCKING: over the ceiling the
    # shadow is skipped and counted, never queued, because the authoritative answer must not wait
    # on a cache that is already struggling.
    MAX_SHADOWS = 8

    def __init__(self, settings=None, factory=None, log=None, clock=time.time, max_shadows=None):
        self._settings = settings
        self.factory = factory
        self._log = log or (lambda line: None)
        self.clock = clock
        self.counters = Counters()
        self.threads = []
        self.max_shadows = self.MAX_SHADOWS if max_shadows is None else max_shadows
        self._lock = threading.Lock()

    def log(self, line):
        """Diagnostics must never become the failure. Copilot, PR 323: a log callback raising
        OSError after a committed mirror made write() raise, so a caller would retry a write that
        had already landed - the diagnostic turning a success into a duplicate. Everything here is
        observation, so a logger that throws is swallowed and counted."""
        try:
            self._log(line)
        except Exception:
            self.counters.bump("log_errors")

    def settings(self):
        """Fresh settings for THIS operation, so the off-switch takes effect mid-run.

        "Without a redeploy" belongs to the process reading the file, not to a deployed job whose
        copy is in its image. The Settings docstring has the full table."""
        return self._settings if self._settings is not None else Settings()

    def _admit(self, thread) -> bool:
        """Reap finished workers and admit this one only if there is room. Both under one lock, so
        two concurrent reads cannot each see room for the last slot."""
        with self._lock:
            self.threads = [t for t in self.threads if t.is_alive()]
            if len(self.threads) >= self.max_shadows:
                return False
            self.threads.append(thread)
            return True

    # --- reads ---------------------------------------------------------------------------

    def read(self, key, authoritative, compare=None, wait=False):
        """Return what `authoritative()` returns. Always. Shadow-read Redis beside it when enabled.

        His words were "background read from Redis first". Read FIRST in the sense of exercising that
        path on every operation; never FIRST in the sense of answering from it. The answer is the old
        path's, so a wrong or stale cache cannot reach him - it can only produce a divergence line.
        `wait=True` is for tests and for the VERIFY run, so the comparison can be asserted."""
        settings = self.settings()
        answer = authoritative()
        if not settings.live() or not settings.shadow_reads:
            self.counters.bump("skipped_off")
            return answer
        thread = threading.Thread(target=self._shadow, args=(settings, key, answer, compare),
                                  name="redis-shadow", daemon=True)
        if not self._admit(thread):
            self.counters.bump("shadow_shed")
            return answer
        try:
            thread.start()
        except (RuntimeError, OSError) as error:
            # Out of threads. The authoritative answer is already in hand and is what the caller
            # asked for; a failure to start an OBSERVER must not become the caller's failure.
            with self._lock:
                self.threads = [t for t in self.threads if t is not thread]
            self.counters.bump("shadow_errors")
            self.log("redis shadow could not start for %s: %s" % (key, type(error).__name__))
            return answer
        if wait:
            thread.join(timeout=PROBE_SECONDS * 3)
        return answer

    def _shadow(self, settings, key, answer, compare):
        self.counters.bump("shadow_reads")
        try:
            conn = client(settings, self.factory)
            if conn is None:
                self.counters.bump("shadow_errors")
                return
            got = conn.get(settings.key_prefix + key)
        except Exception as error:
            self.counters.bump("shadow_errors")
            self.log("redis shadow read failed for %s: %s" % (key, type(error).__name__))
            return
        if got is None:
            self.counters.bump("shadow_misses")
            return
        self.counters.bump("shadow_hits")
        same = (compare or self._same)(got, answer)
        self.counters.bump("agreed" if same else "diverged")
        if not same:
            # The divergence is the product of the dual-run. It names sizes, never contents: the board
            # carries his and other people's words and they do not belong in a cache log.
            self.log("redis DIVERGED on %s: cached %d chars, authoritative %d chars; the authoritative "
                     "answer was served" % (key, len(str(got)), len(str(answer))))

    @staticmethod
    def _same(cached, answer) -> bool:
        if isinstance(answer, (str, bytes)):
            return str(cached) == (answer.decode() if isinstance(answer, bytes) else answer)
        try:
            return json.loads(cached) == json.loads(json.dumps(answer, default=str))
        except (ValueError, TypeError):
            return False

    # --- writes --------------------------------------------------------------------------

    def write(self, key, value, authoritative, ttl=None):
        """Do the authoritative write, then mirror it. The mirror can never fail the caller.

        Order matters and is not arbitrary: the authoritative write goes first, so a mirror that
        succeeds while the real write fails cannot leave a value in the cache that was never
        committed. A mirror that fails after a committed write only leaves the cache stale, which the
        shadow read above is built to notice."""
        result = authoritative()
        settings = self.settings()
        if not settings.live() or not settings.write_through:
            self.counters.bump("skipped_off")
            return result
        try:
            conn = client(settings, self.factory)
            if conn is None:
                self.counters.bump("mirror_errors")
                return result
            payload = value if isinstance(value, str) else json.dumps(value, default=str)
            if ttl:
                conn.setex(settings.key_prefix + key, int(ttl), payload)
            else:
                conn.set(settings.key_prefix + key, payload)
            self.counters.bump("mirrors")
        except Exception as error:
            self.counters.bump("mirror_errors")
            self.log("redis mirror failed for %s: %s" % (key, type(error).__name__))
        return result

    def drain(self, seconds=PROBE_SECONDS * 3):
        """Wait for outstanding shadow reads. For the VERIFY run and for tests, never on a hot path."""
        with self._lock:
            pending = list(self.threads)
        for thread in pending:
            thread.join(timeout=seconds)
        with self._lock:
            self.threads = [t for t in self.threads if t.is_alive()]
        return self.counters.snapshot()


# THE KEYS AN ENTRYPOINT LOGS, named once.
#
# Renaming `enabled` to `enabled_in_file` here broke all three Cloud Run entrypoints with a KeyError
# at startup, and no suite caught it: every test exercises status() directly and none of them tested
# what the entrypoints READ out of it. A dict lookup across a module boundary is an interface. This
# is that interface, and tests/test_redis_dual.py asserts every name in it exists.
STATUS_FOR_LOG = ("dual_run_live", "connect_permitted", "settings_file_readable",
                  "enabled_in_file", "would_attempt_connection", "auth_string_found",
                  "auth_source", "ca_cert_configured")


def status(settings=None) -> dict:
    """What is configured and what is reachable. Nothing secret: whether AUTH was FOUND, never its value."""
    settings = settings or Settings()
    live = settings.live()
    may_connect = settings.connect_ok()
    found = bool(auth_string()) if may_connect else None
    try:
        import redis                                    # noqa: F401
        library = True
    except ImportError:
        library = False
    return {
        "settings_file": str(settings.path),
        "settings_file_present": settings.path.is_file(),
        # The raw value AND the effective one. A reader that sees only the raw value cannot tell a
        # dual-run that is off from one the file has vetoed, and the difference is the whole fix.
        "enabled_in_file": bool(settings._data.get("enabled")),
        "settings_file_readable": settings.file_ok,
        "dual_run_live": live,
        "connect_permitted": may_connect,
        "host": settings.host or "(none)",
        "port": settings.port,
        "tls": bool(settings.tls),
        "ca_cert_configured": bool(settings.ca_cert_path),
        "redis_library_installed": library,
        "would_attempt_connection": may_connect,
        "reachable": reachable(settings.host, settings.port) if may_connect else None,
        "auth_string_found": found,
        "auth_source": ("file" if os.environ.get(AUTH_FILE_ENV) else "gcloud"),
        "shadow_reads": bool(settings.shadow_reads),
        "write_through": bool(settings.write_through),
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    what = argv[0] if argv else "status"
    if what == "status":
        for key, value in status().items():
            print("%-26s %s" % (key, value))
        return 0
    if what == "selftest":
        return selftest()
    print(__doc__)
    return 2


def selftest() -> int:
    """Exercise the wrapper against a fake client, with no network and no secret.

    Here rather than only in tests/ so the scaffold can be shown working on a box that has no redis
    library, no VPC egress and no reachable host - which is every box we own today."""
    class Fake:
        def __init__(self, store=None, fail=False):
            self.store, self.fail, self.sets = dict(store or {}), fail, []

        def get(self, key):
            if self.fail:
                raise RuntimeError("fake get failure")
            return self.store.get(key)

        def set(self, key, value):
            if self.fail:
                raise RuntimeError("fake set failure")
            self.store[key] = value
            self.sets.append(key)

        def setex(self, key, ttl, value):
            self.set(key, value)

    lines = []
    # BOTH switches on: client() gates on `connect`, DualRun on `enabled`. A fixture setting one
    # of them would exercise a configuration no deployed job has.
    on = Settings({"enabled": True, "connect": True, "host": "10.0.0.1"})
    off = Settings({"enabled": False, "connect": False, "host": "10.0.0.1"})

    # 1. OFF attempts nothing.
    touched = []
    dual = DualRun(settings=off, factory=lambda s: touched.append(1), log=lines.append)
    assert dual.read("k", lambda: "authoritative") == "authoritative"
    dual.write("k", "v", lambda: "written")
    assert touched == [], "a disabled dual-run touched the client factory"

    # 2. ON, and Redis disagrees: the authoritative answer is still served.
    fake = Fake({on.key_prefix + "k": "STALE"})
    dual = DualRun(settings=on, factory=lambda s: fake, log=lines.append)
    assert dual.read("k", lambda: "fresh", wait=True) == "fresh"
    counts = dual.drain()
    assert counts["diverged"] == 1, counts
    assert any("DIVERGED" in line for line in lines), lines

    # 3. A mirror failure never reaches the caller.
    broken = Fake(fail=True)
    dual = DualRun(settings=on, factory=lambda s: broken, log=lines.append)
    assert dual.write("k", "v", lambda: "committed") == "committed"
    assert dual.counters.snapshot()["mirror_errors"] == 1

    # 4. Write-through writes the mirror, and the authoritative write runs first.
    order, fake = [], Fake()
    dual = DualRun(settings=on, factory=lambda s: fake, log=lines.append)
    dual.write("k", "v", lambda: order.append("authoritative"))
    assert order == ["authoritative"] and fake.sets == [on.key_prefix + "k"], (order, fake.sets)

    # 5. The off-switch takes effect mid-run, with no restart.
    flip = {"enabled": True, "connect": True, "host": "10.0.0.1"}
    live = DualRun(factory=lambda s: fake, log=lines.append)
    live._settings = None

    class Flipping(DualRun):
        def settings(self):
            return Settings(dict(flip))

    live = Flipping(factory=lambda s: fake, log=lines.append)
    before = live.counters.snapshot()["skipped_off"]
    live.read("k", lambda: "x", wait=True)
    flip["enabled"] = False
    live.read("k", lambda: "x", wait=True)
    assert live.counters.snapshot()["skipped_off"] == before + 1, "the off-switch did not take effect"

    # "works without a restart" was the overclaim Codex caught, and a selftest is the worst place
    # to leave one: it is the line somebody quotes. Bounded to what this actually proves - the
    # switch takes effect inside THIS process, mid-run, because the settings are re-read on every
    # call. Whether an operator can reach that file on a deployed job is a separate question, and
    # the Settings docstring answers it.
    print("SELFTEST OK: 5 properties - off attempts nothing; the authoritative answer always wins; a "
          "mirror failure never reaches the caller; the authoritative write runs first; and the "
          "off-switch takes effect mid-run IN THIS PROCESS, with no restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
