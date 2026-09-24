"""Focused offline tests for the Studio email OTP domain service."""
from __future__ import annotations

import copy
import json
import re
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "cloud" / "studio-controller"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CONTROLLER))

from app.auth import (  # noqa: E402
    ACCEPTANCE_SECONDS,
    AuthService,
    MAX_VERIFY_ATTEMPTS,
    OTP_TTL_SECONDS,
    SEND_WINDOW_SECONDS,
)
from scripts.state_store import Conflict  # noqa: E402


SECRET = "test-auth-secret-with-at-least-thirty-two-bytes"
EMAIL = "operator@example.com"
CLIENT = "hashed-by-service-client-key"


class MemoryStore:
    """Thread-safe store with the same load/save CAS contract as state_store."""

    def __init__(self):
        self.data = {}
        self.generation = {}
        self.lock = threading.Lock()

    def load(self, name):
        with self.lock:
            if name not in self.data:
                return {}, None
            return copy.deepcopy(self.data[name]), str(self.generation[name])

    def save(self, name, state, token):
        with self.lock:
            current = str(self.generation[name]) if name in self.data else None
            if current != token:
                raise Conflict("lost CAS")
            self.data[name] = copy.deepcopy(state)
            self.generation[name] = self.generation.get(name, 0) + 1
            return str(self.generation[name])


class Clock:
    def __init__(self, value=1_000):
        self.value = value

    def __call__(self):
        return self.value


class Sequence:
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.value += 1
            return "%032x" % self.value


class Sender:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.lock = threading.Lock()

    def __call__(self, email, code):
        with self.lock:
            self.calls.append((email, code))
        if self.fail:
            raise RuntimeError("synthetic delivery failure")


def service(*, store=None, clock=None, sender=None, otp="123456", attempts=20,
            **acceptance):
    store = store or MemoryStore()
    clock = clock or Clock()
    sender = sender or Sender()
    auth = AuthService(
        store,
        {EMAIL},
        SECRET,
        sender,
        clock=clock,
        otp_generator=lambda: otp,
        challenge_id_generator=Sequence(),
        cas_attempts=attempts,
        **{"dispatch": lambda work: work(), "wait_until": lambda deadline: None,
           **acceptance},
    )
    return auth, store, clock, sender


class AuthTests(unittest.TestCase):
    def test_acceptance_does_not_wait_for_held_sender(self):
        entered, release, completed = (threading.Event() for _ in range(3))
        waits = []

        def sender(email, code):
            entered.set()
            release.wait()
            completed.set()

        def acceptance_deadline(deadline):
            waits.append(deadline)
            self.assertTrue(entered.wait(5), "sender must be executing")
            self.assertFalse(completed.is_set())

        # Exercise the production bounded dispatcher. The injected clock/wait
        # proves ordering, without wall-clock latency assertions or sleeps.
        from app.auth import _dispatch_start
        auth, _, _, _ = service(sender=sender, dispatch=_dispatch_start,
                                monotonic=lambda: 50, wait_until=acceptance_deadline)
        try:
            result = auth.start(EMAIL, CLIENT)
            unknown = auth.start("other@example.com", CLIENT)
            self.assertFalse(completed.is_set())
            self.assertEqual({"challenge_id", "expires_in"}, set(result))
            self.assertEqual(set(result), set(unknown))
            self.assertEqual([50 + ACCEPTANCE_SECONDS] * 2, waits)
        finally:
            release.set()
            self.assertTrue(completed.wait(5))

    def test_acceptance_does_not_wait_for_held_storage(self):
        entered, release, delivered = (threading.Event() for _ in range(3))

        class HeldStore(MemoryStore):
            def load(self, name):
                entered.set()
                release.wait()
                return super().load(name)

        def acceptance_deadline(deadline):
            self.assertTrue(entered.wait(5))
            self.assertFalse(delivered.is_set())

        from app.auth import _dispatch_start
        auth, _, _, _ = service(store=HeldStore(), dispatch=_dispatch_start,
                                sender=lambda email, code: delivered.set(),
                                wait_until=acceptance_deadline)
        try:
            accepted = auth.start(EMAIL, CLIENT)
            self.assertEqual({"challenge_id", "expires_in"}, set(accepted))
            self.assertFalse(delivered.is_set())
        finally:
            release.set()
            self.assertTrue(delivered.wait(5))

    def test_acceptance_precedes_all_membership_and_storage_work(self):
        pending, deadlines = [], []
        auth, store, _, sender = service(dispatch=pending.append,
                                        monotonic=lambda: 100,
                                        wait_until=deadlines.append)
        accepted = [auth.start(email, CLIENT) for email in
                    (EMAIL, "other@example.com", EMAIL.upper())]
        self.assertEqual({}, store.data)
        self.assertEqual([], sender.calls)
        self.assertEqual(3, len(pending))
        self.assertEqual([100 + ACCEPTANCE_SECONDS] * 3, deadlines)
        for work in pending:
            work()
        self.assertEqual([(EMAIL, "123456")], sender.calls)
        self.assertTrue(auth.verify(accepted[0]["challenge_id"], EMAIL,
                                    "123456", CLIENT)["verified"])
        self.assertEqual({"verified": False}, auth.verify(
            accepted[1]["challenge_id"], "other@example.com", "123456", CLIENT))

    def test_refusal_limit_sender_and_storage_failures_share_acceptance_deadline(self):
        deadlines = []
        auth, store, _, sender = service(monotonic=lambda: 200,
                                        wait_until=deadlines.append)
        responses = [auth.start(EMAIL, CLIENT) for _ in range(4)]
        responses += [auth.start("unknown@example.com", CLIENT), auth.start(EMAIL, "")]
        self.assertEqual(3, len(sender.calls))
        failing, _, _, _ = service(sender=Sender(fail=True), monotonic=lambda: 200,
                                   wait_until=deadlines.append)
        responses.append(failing.start(EMAIL, CLIENT))

        class BrokenStore(MemoryStore):
            def load(self, name):
                raise RuntimeError("storage unavailable")

        failing, _, _, _ = service(store=BrokenStore(), monotonic=lambda: 200,
                                   wait_until=deadlines.append)
        responses.append(failing.start(EMAIL, CLIENT))
        self.assertEqual([200 + ACCEPTANCE_SECONDS] * len(responses), deadlines)
        self.assertTrue(all(set(item) == {"challenge_id", "expires_in"}
                            for item in responses))

    def test_dispatcher_saturation_is_bounded_and_returns_decoys(self):
        from unittest import mock
        from app import auth as auth_module

        pending, deadlines = [], []

        class HeldPool:
            def submit(self, work):
                pending.append(work)

        with mock.patch.object(auth_module, "_START_POOL", HeldPool()), \
             mock.patch.object(auth_module, "_START_SLOTS", threading.BoundedSemaphore(2)):
            auth, store, _, sender = service(dispatch=auth_module._dispatch_start,
                                            monotonic=lambda: 300,
                                            wait_until=deadlines.append)
            accepted = [auth.start(email, CLIENT) for email in
                        (EMAIL, "other@example.com", EMAIL, "other@example.com")]
            self.assertEqual(2, len(pending))
            self.assertEqual([300 + ACCEPTANCE_SECONDS] * 4, deadlines)
            for work in pending:
                work()
            self.assertEqual([(EMAIL, "123456")], sender.calls)
            self.assertNotIn("studio_auth_challenge_" + accepted[2]["challenge_id"], store.data)

    def test_allowlist_is_exact_lowercase_and_start_is_enumeration_safe(self):
        auth, store, _, sender = service()
        allowed = auth.start(EMAIL, CLIENT)
        uppercase = auth.start("Operator@example.com", CLIENT)
        unknown = auth.start("other@example.com", CLIENT)
        no_client = auth.start(EMAIL, "")

        for result in (allowed, uppercase, unknown, no_client):
            self.assertEqual({"challenge_id", "expires_in"}, set(result))
            self.assertEqual(OTP_TTL_SECONDS, result["expires_in"])
            self.assertRegex(result["challenge_id"], r"^[0-9a-f]{32}$")
        self.assertEqual([(EMAIL, "123456")], sender.calls)
        self.assertEqual(2, len(store.data))  # one rate record and one challenge
        with self.assertRaisesRegex(ValueError, "exact lowercase"):
            AuthService(store, {"Operator@example.com"}, SECRET, sender)

    def test_only_hashes_are_persisted_and_success_returns_stable_subject(self):
        auth, store, _, sender = service()
        first = auth.start(EMAIL, CLIENT)
        code = sender.calls[-1][1]
        serialized = json.dumps(store.data, sort_keys=True)
        self.assertNotIn(EMAIL, serialized)
        self.assertNotIn(CLIENT, serialized)
        self.assertNotIn(code, serialized)
        self.assertTrue(all(name.startswith("studio_auth_") for name in store.data))

        verified = auth.verify(first["challenge_id"], EMAIL, code, CLIENT)
        self.assertTrue(verified["verified"])
        self.assertRegex(verified["subject_hash"], r"^[0-9a-f]{64}$")
        self.assertNotIn(EMAIL, json.dumps(verified))
        self.assertEqual({"verified": False}, auth.verify(first["challenge_id"], EMAIL, code, CLIENT))

        second = auth.start(EMAIL, CLIENT)
        verified_again = auth.verify(second["challenge_id"], EMAIL, sender.calls[-1][1], CLIENT)
        self.assertEqual(verified["subject_hash"], verified_again["subject_hash"])

    def test_challenge_is_bound_to_email_and_client_and_every_attempt_counts(self):
        auth, store, _, sender = service()
        started = auth.start(EMAIL, CLIENT)
        code = sender.calls[-1][1]
        self.assertEqual(
            {"verified": False},
            auth.verify(started["challenge_id"], "other@example.com", code, CLIENT),
        )
        self.assertEqual(
            {"verified": False},
            auth.verify(started["challenge_id"], EMAIL, code, "other-client"),
        )
        challenge = store.data["studio_auth_challenge_" + started["challenge_id"]]
        self.assertEqual(2, challenge["attempts"])
        self.assertTrue(auth.verify(started["challenge_id"], EMAIL, code, CLIENT)["verified"])

    def test_fifth_attempt_can_succeed_but_five_failures_lock_the_challenge(self):
        auth, store, _, sender = service()
        started = auth.start(EMAIL, CLIENT)
        for _ in range(MAX_VERIFY_ATTEMPTS - 1):
            self.assertEqual(
                {"verified": False},
                auth.verify(started["challenge_id"], EMAIL, "000000", CLIENT),
            )
        self.assertTrue(auth.verify(started["challenge_id"], EMAIL, sender.calls[-1][1], CLIENT)["verified"])

        locked = auth.start(EMAIL, CLIENT)
        for _ in range(MAX_VERIFY_ATTEMPTS):
            self.assertEqual(
                {"verified": False},
                auth.verify(locked["challenge_id"], EMAIL, "000000", CLIENT),
            )
        self.assertEqual(
            {"verified": False},
            auth.verify(locked["challenge_id"], EMAIL, sender.calls[-1][1], CLIENT),
        )
        record = store.data["studio_auth_challenge_" + locked["challenge_id"]]
        self.assertEqual(MAX_VERIFY_ATTEMPTS, record["attempts"])

    def test_ten_minute_ttl_is_fail_closed_at_the_boundary(self):
        auth, _, clock, sender = service()
        before = auth.start(EMAIL, CLIENT)
        clock.value += OTP_TTL_SECONDS - 1
        self.assertTrue(auth.verify(before["challenge_id"], EMAIL, sender.calls[-1][1], CLIENT)["verified"])

        after = auth.start(EMAIL, CLIENT)
        clock.value += OTP_TTL_SECONDS
        self.assertEqual(
            {"verified": False},
            auth.verify(after["challenge_id"], EMAIL, sender.calls[-1][1], CLIENT),
        )

    def test_three_sends_per_sliding_window_with_cas_and_enumeration_safe_limit(self):
        auth, store, clock, sender = service()
        results = [auth.start(EMAIL, CLIENT) for _ in range(4)]
        self.assertEqual(3, len(sender.calls))
        self.assertTrue(all(set(item) == {"challenge_id", "expires_in"} for item in results))

        clock.value += SEND_WINDOW_SECONDS - 1
        auth.start(EMAIL, CLIENT)
        self.assertEqual(3, len(sender.calls))
        clock.value += 1
        auth.start(EMAIL, CLIENT)
        self.assertEqual(4, len(sender.calls))

        rate_names = [name for name in store.data if name.startswith("studio_auth_rate_")]
        self.assertEqual(1, len(rate_names))
        self.assertNotIn(EMAIL, rate_names[0])

    def test_concurrent_starts_never_overrun_send_limit(self):
        auth, _, _, sender = service()
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: auth.start(EMAIL, CLIENT), range(20)))
        self.assertEqual(3, len(sender.calls))
        self.assertEqual(20, len(results))
        self.assertTrue(all(set(item) == {"challenge_id", "expires_in"} for item in results))

    def test_concurrent_correct_verification_is_single_use(self):
        auth, _, _, sender = service()
        started = auth.start(EMAIL, CLIENT)
        code = sender.calls[-1][1]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: auth.verify(started["challenge_id"], EMAIL, code, CLIENT),
                range(2),
            ))
        self.assertEqual(1, sum(1 for item in results if item["verified"]))

    def test_default_generator_is_six_ascii_digits_and_sender_failure_is_not_exposed(self):
        store = MemoryStore()
        sender = Sender(fail=True)
        auth = AuthService(
            store,
            {EMAIL},
            SECRET,
            sender,
            clock=Clock(),
            challenge_id_generator=Sequence(),
            dispatch=lambda work: work(),
            wait_until=lambda deadline: None,
        )
        result = auth.start(EMAIL, CLIENT)
        self.assertEqual({"challenge_id", "expires_in"}, set(result))
        self.assertEqual(1, len(sender.calls))
        self.assertTrue(re.fullmatch(r"[0-9]{6}", sender.calls[0][1]))
        self.assertNotIn(sender.calls[0][1], json.dumps(store.data))


if __name__ == "__main__":
    unittest.main()
