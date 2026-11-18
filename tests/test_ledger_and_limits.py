"""Tests 14-25: the duplicate-prevention ledger, rate limiter and instance lock."""

import os
import tempfile
import threading
import unittest
from pathlib import Path

from ccc.config import Limits
from ccc.ledger import AlreadyRecorded, Ledger, SendKey
from ccc.lock import AlreadyRunning, InstanceLock
from ccc.rate_limiter import RateLimiter, RateLimitExceeded

KEY = SendKey("announcement", 101, 5000, 102)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.db"
        self.ledger = Ledger(self.path)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    # 14
    def test_14_reserve_creates_pending_row(self):
        row_id = self.ledger.reserve(KEY, "Welcome")
        row = self.ledger.find(KEY)
        self.assertEqual((row.id, row.status, row.title), (row_id, "pending", "Welcome"))

    # 15
    def test_15_second_reservation_of_same_send_refused(self):
        self.ledger.reserve(KEY, "Welcome")
        with self.assertRaises(AlreadyRecorded):
            self.ledger.reserve(KEY, "Welcome")
        self.assertEqual(len(self.ledger.all_rows()), 1)

    # 16
    def test_16_sent_row_blocks_resend(self):
        rid = self.ledger.reserve(KEY, "Welcome")
        self.ledger.mark_sent(rid, 777)
        with self.assertRaises(AlreadyRecorded) as cm:
            self.ledger.reserve(KEY, "Welcome")
        self.assertEqual(cm.exception.row.created_item_id, 777)
        # a different target course is a different send and is allowed
        self.ledger.reserve(SendKey("announcement", 101, 5000, 103), "Welcome")

    # 17
    def test_17_definitely_failed_row_may_be_retried(self):
        rid = self.ledger.reserve(KEY, "Welcome")
        self.ledger.mark_failed(rid, "HTTP 403")
        self.assertEqual(self.ledger.reserve(KEY, "Welcome"), rid)
        self.assertEqual(self.ledger.find(KEY).status, "pending")

    # 18
    def test_18_ledger_survives_restart(self):
        rid = self.ledger.reserve(KEY, "Welcome")
        self.ledger.mark_sent(rid, 1)
        self.ledger.close()
        self.ledger = Ledger(self.path)
        with self.assertRaises(AlreadyRecorded):
            self.ledger.reserve(KEY, "Welcome")

    # 19
    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_19_ledger_file_is_private_to_owner(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    # 20
    def test_20_release_allows_resend(self):
        rid = self.ledger.reserve(KEY, "Welcome")
        self.assertTrue(self.ledger.release(rid))
        self.assertIsNone(self.ledger.find(KEY))
        self.ledger.reserve(KEY, "Welcome")

    # 21
    def test_21_concurrent_reservations_only_one_wins(self):
        wins, losses = [], []
        barrier = threading.Barrier(8)

        def worker():
            led = Ledger(self.path)
            barrier.wait()
            try:
                led.reserve(KEY, "Welcome")
                wins.append(1)
            except AlreadyRecorded:
                losses.append(1)
            finally:
                led.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((len(wins), len(losses)), (1, 7))


class RateLimitAndLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.db"
        self.clock = Clock()
        self.ledger = Ledger(self.path, clock=self.clock)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def limiter(self, **kw):
        return RateLimiter(self.ledger, Limits(**kw), clock=self.clock, sleep=self.clock.sleep)

    # 22
    def test_22_minimum_spacing_between_posts(self):
        rl = self.limiter(min_seconds_between_posts=3.0)
        rl.acquire()
        self.clock.t += 1.0
        rl.acquire()
        self.assertEqual(self.clock.slept, [2.0])
        self.clock.t += 10.0          # plenty of time passed: no wait at all
        rl.acquire()
        self.assertEqual(self.clock.slept, [2.0])

    # 23
    def test_23_per_run_cap(self):
        rl = self.limiter(min_seconds_between_posts=0, max_posts_per_run=2)
        rl.acquire()
        rl.acquire()
        with self.assertRaises(RateLimitExceeded):
            rl.acquire()
        rl.start_run()
        rl.acquire()                  # a new run may continue

    # 24
    def test_24_hourly_cap_survives_restart(self):
        rl = self.limiter(min_seconds_between_posts=0, max_posts_per_hour=3)
        for _ in range(3):
            rl.acquire()
        rl2 = self.limiter(min_seconds_between_posts=0, max_posts_per_hour=3)   # "restarted app"
        with self.assertRaises(RateLimitExceeded):
            rl2.acquire()
        self.clock.t += 3601
        rl2.acquire()                 # window rolled over

    # 25
    def test_25_only_one_app_instance_can_run(self):
        lock_path = Path(self.tmp.name) / "app.lock"
        first = InstanceLock(lock_path).acquire()
        try:
            with self.assertRaises(AlreadyRunning):
                InstanceLock(lock_path).acquire()
        finally:
            first.release()
        InstanceLock(lock_path).acquire().release()


if __name__ == "__main__":
    unittest.main()
