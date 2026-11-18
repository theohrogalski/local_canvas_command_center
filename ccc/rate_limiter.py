"""Rate limiting for POSTs. Uses the ledger's attempt log so limits persist
across restarts (closing and reopening the app does not reset them)."""

from __future__ import annotations

import time

from .config import Limits
from .ledger import Ledger


class RateLimitExceeded(Exception):
    pass


class RateLimiter:
    def __init__(self, ledger: Ledger, limits: Limits, clock=time.time, sleep=time.sleep,
                 channel: str = "post"):
        self.ledger = ledger
        self.channel = channel
        self.limits = limits
        self.clock = clock
        self.sleep = sleep
        self.posts_this_run = 0

    def start_run(self):
        self.posts_this_run = 0

    def acquire(self):
        """Block until a POST is allowed, or raise RateLimitExceeded.
        Records the attempt (call this immediately before the POST)."""
        if self.posts_this_run >= self.limits.max_posts_per_run:
            raise RateLimitExceeded(
                f"Per-run limit reached ({self.limits.max_posts_per_run}). Start a new run to continue.")
        now = self.clock()
        if self.ledger.attempts_since(now - 3600, self.channel) >= self.limits.max_posts_per_hour:
            raise RateLimitExceeded(
                f"Hourly limit reached ({self.limits.max_posts_per_hour} in the last 60 minutes).")
        last = self.ledger.last_attempt(self.channel)
        if last is not None:
            # min() guards against a clock that moved backwards.
            wait = min(self.limits.min_seconds_between_posts,
                       self.limits.min_seconds_between_posts - (now - last))
            if wait > 0:
                self.sleep(wait)
        self.ledger.record_attempt(self.clock(), self.channel)
        self.posts_this_run += 1
