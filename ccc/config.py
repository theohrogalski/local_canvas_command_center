"""Settings and safety limits.

Limits can be lowered (never need to be raised) with environment variables:
  CCC_MIN_SECONDS_BETWEEN_POSTS, CCC_MAX_POSTS_PER_RUN, CCC_MAX_POSTS_PER_HOUR
The data directory (ledger + lock file) defaults to ~/.canvas_command_center
and can be moved with CCC_HOME.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def app_dir() -> Path:
    path = Path(os.environ.get("CCC_HOME", str(Path.home() / ".canvas_command_center")))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


@dataclass(frozen=True)
class Limits:
    # Wait at least this long between any two POSTs (persisted across restarts).
    min_seconds_between_posts: float = 3.0
    # Hard cap on POSTs during one "Send" click.
    max_posts_per_run: int = 25
    # Hard cap on POSTs in any rolling 60-minute window (persisted across restarts).
    max_posts_per_hour: int = 40

    @classmethod
    def from_env(cls) -> "Limits":
        d = cls()

        def num(name, default, cast):
            raw = os.environ.get(name)
            if raw is None:
                return default
            value = cast(raw)
            if value < 0:
                raise ValueError(f"{name} must be >= 0")
            return value

        return cls(
            min_seconds_between_posts=num("CCC_MIN_SECONDS_BETWEEN_POSTS", d.min_seconds_between_posts, float),
            max_posts_per_run=num("CCC_MAX_POSTS_PER_RUN", d.max_posts_per_run, int),
            max_posts_per_hour=num("CCC_MAX_POSTS_PER_HOUR", d.max_posts_per_hour, int),
        )


# Grading edits are small, idempotent "set" operations (a course may need dozens),
# so they get their own, separate budget. They never use up the announcement budget.
GRADING_DEFAULTS = Limits(min_seconds_between_posts=0.5, max_posts_per_run=400, max_posts_per_hour=1000)


def grading_limits_from_env() -> Limits:
    d = GRADING_DEFAULTS

    def num(name, default, cast):
        raw = os.environ.get(name)
        if raw is None:
            return default
        value = cast(raw)
        if value < 0:
            raise ValueError(f"{name} must be >= 0")
        return value

    return Limits(
        min_seconds_between_posts=num("CCC_GRADING_MIN_SECONDS_BETWEEN_EDITS", d.min_seconds_between_posts, float),
        max_posts_per_run=num("CCC_GRADING_MAX_EDITS_PER_RUN", d.max_posts_per_run, int),
        max_posts_per_hour=num("CCC_GRADING_MAX_EDITS_PER_HOUR", d.max_posts_per_hour, int),
    )


# File uploads add content students may see: same caution as announcements.
UPLOAD_DEFAULTS = Limits(min_seconds_between_posts=3.0, max_posts_per_run=50, max_posts_per_hour=100)
# Publish/unpublish flips one switch per item; separate budget.
VISIBILITY_DEFAULTS = Limits(min_seconds_between_posts=0.5, max_posts_per_run=100, max_posts_per_hour=300)


def _limits_from_env(prefix: str, d: Limits) -> Limits:
    def num(name, default, cast):
        raw = os.environ.get(prefix + name)
        if raw is None:
            return default
        value = cast(raw)
        if value < 0:
            raise ValueError(f"{prefix + name} must be >= 0")
        return value

    return Limits(num("MIN_SECONDS_BETWEEN", d.min_seconds_between_posts, float),
                  num("MAX_PER_RUN", d.max_posts_per_run, int),
                  num("MAX_PER_HOUR", d.max_posts_per_hour, int))


def upload_limits_from_env() -> Limits:
    return _limits_from_env("CCC_UPLOAD_", UPLOAD_DEFAULTS)


def visibility_limits_from_env() -> Limits:
    return _limits_from_env("CCC_PUBLISH_", VISIBILITY_DEFAULTS)
