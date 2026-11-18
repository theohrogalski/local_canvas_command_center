"""Planning and sending: the logic between the GUI and the Canvas client.

Workflow:
  1. build_plan()  - read-only. Fetches the source item, builds the exact form
                     that will be posted, and decides per target course whether
                     to SEND or SKIP (and why).
  2. execute()     - for each SEND row: re-check ledger + Canvas, reserve in the
                     ledger, wait for the rate limiter, POST once, record result.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .canvas_client import CanvasError, SafeCanvasClient
from .config import Limits
from .ledger import AlreadyRecorded, Ledger, SendKey
from .rate_limiter import RateLimiter, RateLimitExceeded

KINDS = ("announcement", "assignment")

# Submission types that can be recreated faithfully with a plain create call.
SUPPORTED_SUBMISSION_TYPES = {
    "none", "on_paper", "online_text_entry", "online_url", "online_upload", "media_recording",
}


class PlanError(Exception):
    """The request cannot be planned (nothing was sent)."""


@dataclass(frozen=True)
class Target:
    course_id: int
    name: str
    published: bool = True


@dataclass
class PlanRow:
    target: Target
    action: str          # "send" or "skip"
    reason: str = ""
    warnings: list = field(default_factory=list)


@dataclass
class Plan:
    kind: str
    source_course_id: int
    source_item_id: int
    title: str
    form: list            # exact form fields that will be POSTed
    rows: list            # list[PlanRow]
    warnings: list = field(default_factory=list)

    @property
    def to_send(self):
        return [r for r in self.rows if r.action == "send"]


@dataclass
class Result:
    target: Target
    outcome: str         # sent | dry-run | skipped | failed | unknown | not-sent
    message: str = ""
    created_id: int | None = None


def normalise_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "")).strip().casefold()


# ----------------------------------------------------------------- payloads
def announcement_form(src: dict) -> list:
    title = (src.get("title") or "").strip()
    if not title:
        raise PlanError("The source announcement has no title.")
    return [
        ("title", title),
        ("message", src.get("message") or ""),
        ("is_announcement", "true"),
        ("published", "true"),   # required for students to see it / be notified
    ]


def assignment_form(src: dict, publish: bool) -> list:
    name = (src.get("name") or "").strip()
    if not name:
        raise PlanError("The source assignment has no name.")
    if src.get("quiz_id") or src.get("is_quiz_assignment"):
        raise PlanError("Quizzes cannot be copied with this tool (use Canvas 'Copy to').")
    sub_types = list(src.get("submission_types") or ["none"])
    unsupported = [t for t in sub_types if t not in SUPPORTED_SUBMISSION_TYPES]
    if unsupported:
        raise PlanError(f"Submission type(s) {', '.join(unsupported)} cannot be copied safely "
                        "(quizzes, graded discussions, external tools and annotations need Canvas 'Copy to').")
    form = [("assignment[name]", name),
            ("assignment[description]", src.get("description") or "")]
    if src.get("points_possible") is not None:
        form.append(("assignment[points_possible]", str(src["points_possible"])))
    if src.get("grading_type"):
        form.append(("assignment[grading_type]", src["grading_type"]))
    for t in sub_types:
        form.append(("assignment[submission_types][]", t))
    for ext in src.get("allowed_extensions") or []:
        form.append(("assignment[allowed_extensions][]", ext))
    for f in ("due_at", "unlock_at", "lock_at"):
        if src.get(f):
            form.append((f"assignment[{f}]", src[f]))
    form.append(("assignment[published]", "true" if publish else "false"))
    return form


def source_link_warning(src: dict, source_course_id: int) -> str | None:
    body = (src.get("message") or "") + (src.get("description") or "")
    if re.search(rf"/courses/{int(source_course_id)}/", body):
        return ("The text links to files/pages inside the source course. Students in the "
                "other courses may not be able to open those links.")
    if src.get("attachments"):
        return "File attachments are not copied - only the text is."
    return None


# ------------------------------------------------------------ the service
class CommandCenter:
    def __init__(self, client: SafeCanvasClient, ledger: Ledger, limits: Limits | None = None,
                 clock=time.time, sleep=time.sleep):
        self.client = client
        self.ledger = ledger
        self.limits = limits or Limits()
        self.limiter = RateLimiter(ledger, self.limits, clock=clock, sleep=sleep)

    # read helpers
    def list_items(self, kind: str, course_id: int) -> list:
        if kind == "announcement":
            return self.client.list_announcements(course_id)
        if kind == "assignment":
            return self.client.list_assignments(course_id)
        raise PlanError(f"Unknown kind {kind!r}")

    def _title_exists(self, kind: str, course_id: int, title: str) -> bool:
        want = normalise_title(title)
        key = "title" if kind == "announcement" else "name"
        return any(normalise_title(i.get(key)) == want for i in self.list_items(kind, course_id))

    def _ledger_block_reason(self, key: SendKey) -> str | None:
        row = self.ledger.find(key)
        if row is None or row.status == "failed":
            return None
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.updated_at))
        if row.status == "sent":
            return f"Already sent by this tool on {when}"
        return (f"A previous send on {when} did not confirm. Check this course in Canvas; "
                f"see docs for 'release' (ledger #{row.id})")

    # ------------------------------------------------------------- planning
    def build_plan(self, kind: str, source_course_id: int, source_item_id: int,
                   targets: list, publish_assignment: bool = True) -> Plan:
        if kind not in KINDS:
            raise PlanError(f"Unknown kind {kind!r}")
        if not targets:
            raise PlanError("Choose at least one target course.")
        if kind == "announcement":
            src = self.client.get_announcement(source_course_id, source_item_id)
            form = announcement_form(src)
            title = src["title"]
        else:
            src = self.client.get_assignment(source_course_id, source_item_id)
            form = assignment_form(src, publish_assignment)
            title = src["name"]

        plan = Plan(kind, int(source_course_id), int(source_item_id), title, form, rows=[])
        w = source_link_warning(src, source_course_id)
        if w:
            plan.warnings.append(w)

        seen = set()
        for t in targets:
            if t.course_id in seen:
                continue  # silently drop duplicates in the selection
            seen.add(t.course_id)
            if t.course_id == int(source_course_id):
                plan.rows.append(PlanRow(t, "skip", "This is the source course"))
                continue
            key = SendKey(kind, plan.source_course_id, plan.source_item_id, t.course_id)
            reason = self._ledger_block_reason(key)
            if reason:
                plan.rows.append(PlanRow(t, "skip", reason))
                continue
            try:
                if self._title_exists(kind, t.course_id, title):
                    plan.rows.append(PlanRow(t, "skip", "An item with this exact title already exists there"))
                    continue
            except CanvasError as e:
                plan.rows.append(PlanRow(t, "skip", f"Could not check course: {e}"))
                continue
            row = PlanRow(t, "send", "Ready")
            if kind == "announcement" and not t.published:
                row.warnings.append("Course is unpublished: students will not be emailed.")
            plan.rows.append(row)

        if len(plan.to_send) > self.limits.max_posts_per_run:
            raise PlanError(f"This would post to {len(plan.to_send)} courses; the per-run limit is "
                            f"{self.limits.max_posts_per_run}. Select fewer courses.")
        return plan

    # ------------------------------------------------------------ execution
    def execute(self, plan: Plan, dry_run: bool = True,
                progress: Callable[[Result], None] | None = None,
                cancel: threading.Event | None = None) -> list:
        results: list = []
        emit = progress or (lambda r: None)
        self.limiter.start_run()
        rows = plan.to_send
        stop_reason = None

        for idx, row in enumerate(rows):
            t = row.target
            if stop_reason or (cancel and cancel.is_set()):
                r = Result(t, "not-sent", stop_reason or "Cancelled before sending")
                results.append(r); emit(r)
                continue
            key = SendKey(plan.kind, plan.source_course_id, plan.source_item_id, t.course_id)

            # Re-check right before sending (things may have changed since preview).
            reason = self._ledger_block_reason(key)
            if reason:
                r = Result(t, "skipped", reason); results.append(r); emit(r); continue
            try:
                if self._title_exists(plan.kind, t.course_id, plan.title):
                    r = Result(t, "skipped", "An item with this exact title already exists there")
                    results.append(r); emit(r); continue
            except CanvasError as e:
                r = Result(t, "skipped", f"Could not check course: {e}"); results.append(r); emit(r); continue

            if dry_run:
                r = Result(t, "dry-run", "Would be sent (dry run - nothing posted)")
                results.append(r); emit(r); continue

            try:
                row_id = self.ledger.reserve(key, plan.title)
            except AlreadyRecorded as e:
                r = Result(t, "skipped", str(e)); results.append(r); emit(r); continue

            try:
                self.limiter.acquire()
            except RateLimitExceeded as e:
                self.ledger.release(row_id)  # nothing was posted, so un-reserve
                stop_reason = f"Not sent: {e}"
                r = Result(t, "not-sent", stop_reason); results.append(r); emit(r)
                continue

            try:
                if plan.kind == "announcement":
                    created = self.client.create_announcement(t.course_id, plan.form)
                else:
                    created = self.client.create_assignment(t.course_id, plan.form)
            except CanvasError as e:
                if e.ambiguous:
                    self.ledger.note_pending(row_id, str(e))
                    stop_reason = "Stopped after an unconfirmed send (safety stop)."
                    r = Result(t, "unknown", f"Outcome unknown - check Canvas before doing anything. {e}")
                else:
                    self.ledger.mark_failed(row_id, str(e))
                    r = Result(t, "failed", str(e))
                    if e.status in (401, 429):
                        stop_reason = f"Stopped: {e}"
                results.append(r); emit(r)
                continue

            created_id = created.get("id") if isinstance(created, dict) else None
            self.ledger.mark_sent(row_id, created_id)
            r = Result(t, "sent", "Posted", created_id); results.append(r); emit(r)

        return results
