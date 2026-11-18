"""Publish or unpublish assignments and files in a course.

The client used here is in "publish" mode: the ONLY edits it can send are
  PUT /api/v1/courses/<id>/assignments/<id>   assignment[published]=true|false
  PUT /api/v1/files/<id>                      locked=true|false
Nothing else about an item can change.

Per item:
  * preview shows current -> new state; items already in that state are skipped
  * assignments that have student submissions cannot be unpublished (Canvas
    refuses this too) - they are skipped with an explanation
  * quizzes and graded discussions are skipped (publish those from their own page)
  * right before the change the item is re-read; if it changed since the preview, it's skipped
  * for assignments, every submission is fingerprinted before and checked after
  * the item is re-read after the change to confirm the new state
  * every change is recorded in the ledger, and History can undo it
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .canvas_client import CanvasError, SafeCanvasClient
from .config import VISIBILITY_DEFAULTS, Limits
from .copier import PlanError, Target
from .grading import submissions_fingerprint
from .ledger import Ledger
from .rate_limiter import RateLimiter, RateLimitExceeded

KINDS = ("assignment", "file")
_SPECIAL = {"online_quiz": "a quiz", "discussion_topic": "a graded discussion"}


def is_published(kind: str, item: dict) -> bool:
    return bool(item.get("published")) if kind == "assignment" else not item.get("locked")


def item_name(kind: str, item: dict) -> str:
    return str(item.get("name") if kind == "assignment" else (item.get("display_name") or item.get("filename")))


def state_word(published: bool) -> str:
    return "Published" if published else "Unpublished"


@dataclass
class VisRow:
    item_id: int
    name: str
    before: bool
    after: bool
    action: str            # change | skip
    reason: str = ""


@dataclass
class VisPlan:
    target: Target
    kind: str
    publish: bool
    rows: list
    warnings: list = field(default_factory=list)

    @property
    def to_change(self):
        return [r for r in self.rows if r.action == "change"]


@dataclass
class VisResult:
    row: VisRow
    outcome: str           # changed | dry-run | skipped | failed | unknown | verify-failed | not-run
    message: str = ""
    stop: bool = False     # True = halt the rest of the run


class VisibilityCenter:
    def __init__(self, client: SafeCanvasClient, ledger: Ledger, limits: Limits | None = None,
                 clock=time.time, sleep=time.sleep):
        if client.mode != "publish":
            raise ValueError("VisibilityCenter needs a client created with mode='publish'")
        self.client = client
        self.ledger = ledger
        self.limits = limits or VISIBILITY_DEFAULTS
        self.limiter = RateLimiter(ledger, self.limits, clock=clock, sleep=sleep, channel="visibility")

    def list_items(self, kind: str, course_id: int) -> list:
        if kind == "assignment":
            return self.client.list_assignments(course_id)
        if kind == "file":
            return self.client.list_course_files(course_id)
        raise PlanError(f"Unknown kind {kind!r}")

    def _read(self, kind, course_id, item_id) -> dict:
        if kind == "assignment":
            return self.client.get_assignment(course_id, item_id)
        return self.client.get_file(item_id)

    def _blocker(self, kind: str, item: dict, publish: bool) -> str | None:
        if kind != "assignment":
            return None
        special = [_SPECIAL[t] for t in (item.get("submission_types") or []) if t in _SPECIAL]
        if special or item.get("quiz_id"):
            return f"This is {special[0] if special else 'a quiz'} - publish or unpublish it from its own page in Canvas"
        if not publish and item.get("has_submitted_submissions"):
            return "Students have already submitted work, so Canvas does not allow unpublishing it"
        return None

    # ------------------------------------------------------------ preview
    def build_plan(self, target: Target, kind: str, item_ids: list, publish: bool) -> VisPlan:
        if kind not in KINDS:
            raise PlanError(f"Unknown kind {kind!r}")
        if not item_ids:
            raise PlanError("Select at least one item.")
        items = {i["id"]: i for i in self.list_items(kind, target.course_id)}
        rows, seen = [], set()
        for iid in item_ids:
            if iid in seen:
                continue
            seen.add(iid)
            item = items.get(iid)
            if item is None:
                rows.append(VisRow(iid, f"#{iid}", publish, publish, "skip", "No longer exists in this course"))
                continue
            before = is_published(kind, item)
            row = VisRow(iid, item_name(kind, item), before, publish, "change", "Ready")
            if before == publish:
                row.action, row.reason = "skip", f"Already {state_word(publish).lower()}"
            else:
                blocker = self._blocker(kind, item, publish)
                if blocker:
                    row.action, row.reason = "skip", blocker
            rows.append(row)
        plan = VisPlan(target, kind, publish, rows)
        if publish and kind == "assignment" and plan.to_change:
            plan.warnings.append("Publishing makes assignments visible to students right away, and Canvas may "
                                 "notify them according to their notification settings.")
        if len(plan.to_change) > self.limits.max_posts_per_run:
            raise PlanError(f"This would change {len(plan.to_change)} items; the per-run limit is "
                            f"{self.limits.max_posts_per_run}.")
        return plan

    def build_undo_plan(self, change) -> VisPlan:
        """Reverse one recorded change (from ledger.visibility_changes())."""
        target = Target(change.course_id, change.course_name)
        return self.build_plan(target, change.kind, [change.item_id], publish=(change.before == "Published"))

    # ------------------------------------------------------------ execute
    def execute(self, plan: VisPlan, dry_run: bool = True,
                progress: Callable[[VisResult], None] | None = None,
                cancel: threading.Event | None = None) -> list:
        emit = progress or (lambda r: None)
        results, stop = [], None
        self.limiter.start_run()
        for row in plan.to_change:
            if stop or (cancel and cancel.is_set()):
                r = VisResult(row, "not-run", stop or "Cancelled")
            else:
                r = self._one(plan, row, dry_run)
                if r.stop or r.outcome in ("unknown", "verify-failed"):
                    stop = f"Stopped after a problem with '{row.name}'."
            results.append(r)
            emit(r)
        return results

    def _record(self, plan, row, status, detail):
        self.ledger.record_visibility(plan.target.course_id, plan.target.name, plan.kind, row.item_id, row.name,
                                      state_word(row.before), state_word(row.after), status, detail)

    def _one(self, plan: VisPlan, row: VisRow, dry_run: bool) -> VisResult:
        cid, kind = plan.target.course_id, plan.kind
        try:
            item = self._read(kind, cid, row.item_id)
        except CanvasError as e:
            return VisResult(row, "skipped", f"Could not read it: {e}")
        if is_published(kind, item) != row.before:
            return VisResult(row, "skipped", "It changed since the preview. Preview again.")
        blocker = self._blocker(kind, item, row.after)
        if blocker:
            return VisResult(row, "skipped", blocker)
        fp_before = None
        if kind == "assignment":
            try:
                fp_before = submissions_fingerprint(self.client.list_assignment_submissions(cid, row.item_id))
            except CanvasError as e:
                return VisResult(row, "skipped", f"Could not read its submissions, so nothing was changed: {e}")
        if dry_run:
            return VisResult(row, "dry-run", f"Would become {state_word(row.after).lower()} (dry run)")
        try:
            self.limiter.acquire()
        except RateLimitExceeded as e:
            return VisResult(row, "failed", f"Not changed: {e}", stop=True)
        try:
            if kind == "assignment":
                self.client.set_assignment_published(cid, row.item_id, row.after)
            else:
                self.client.set_file_locked(row.item_id, not row.after)
        except CanvasError as e:
            if e.ambiguous:
                self._record(plan, row, "unknown", str(e))
                return VisResult(row, "unknown", f"May or may not have changed - check it in Canvas. {e}")
            self._record(plan, row, "failed", str(e))
            return VisResult(row, "failed", f"Canvas refused: {e}", stop=e.status in (401, 429))
        # verify
        try:
            back = self._read(kind, cid, row.item_id)
            fp_after = (submissions_fingerprint(self.client.list_assignment_submissions(cid, row.item_id))
                        if kind == "assignment" else None)
        except CanvasError as e:
            self._record(plan, row, "verify-failed", str(e))
            return VisResult(row, "verify-failed", f"Changed, but could not be checked: {e}")
        problems = []
        if is_published(kind, back) != row.after:
            problems.append(f"it is still {state_word(is_published(kind, back)).lower()}")
        if fp_before != fp_after:
            problems.append("its submissions/grades differ from before")
        if problems:
            self._record(plan, row, "verify-failed", "; ".join(problems))
            return VisResult(row, "verify-failed", "VERIFY FAILED: " + "; ".join(problems))
        self._record(plan, row, "changed", None)
        extra = f" {fp_before[1]} submission(s) verified unchanged." if fp_before else ""
        return VisResult(row, "changed", f"Now {state_word(row.after).lower()}.{extra}")
