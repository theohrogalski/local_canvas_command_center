"""Grade weighting: copy a source course's assignment-group weighting to other
courses, safely, with snapshots, verification and undo.

WHAT CAN CHANGE (and nothing else - enforced by the client's grading allow-list):
  * "Weight final grade based on assignment groups" on/off for a course
  * an assignment group's weight (%)
  * a new assignment group (name + weight)
  * which assignment group an assignment belongs to

WHAT IS NEVER TOUCHED: scores, grades, submissions, comments, points, dates,
assignment content, rubrics, drop rules. Groups are never deleted.
After every course is changed, all of its submissions are re-read and compared
to a fingerprint taken just before the change; any difference stops the run.

How matching works: groups are matched by name, assignments by name (both
ignoring case and extra spaces). Assignments that exist only in the target
course are left exactly where they are.

Workflow (mirrors copier.py):
  build_copy_plan() / build_restore_plan()  - read-only preview
  execute()                                  - per course: re-read, compare with
        the preview (stale -> skip), snapshot to disk, fingerprint submissions,
        apply edits one at a time (rate limited), re-read and verify.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .canvas_client import CanvasError, SafeCanvasClient, fmt_weight
from .config import GRADING_DEFAULTS, Limits
from .copier import PlanError, Target
from .ledger import Ledger
from .rate_limiter import RateLimiter, RateLimitExceeded

TOTAL_TOLERANCE = 0.01

# Submission fields that together represent "what was turned in and how it was graded".
# (Not "seconds_late": Canvas keeps increasing it for missing work as time passes,
#  which would cause false alarms. The late/missing status and deduction ARE checked.)
SUBMISSION_FIELDS = ("id", "assignment_id", "user_id", "score", "grade", "entered_score", "entered_grade",
                     "excused", "workflow_state", "submitted_at", "attempt", "late_policy_status",
                     "points_deducted", "submission_type", "graded_at")
# Assignment fields that must be identical before and after (only the group may change).
ASSIGNMENT_FIELDS = ("id", "name", "points_possible", "grading_type", "submission_types", "due_at",
                     "unlock_at", "lock_at", "published", "omit_from_final_grade", "allowed_extensions")


def norm(name) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def same_weight(a, b) -> bool:
    return abs(float(a or 0) - float(b or 0)) < 1e-6


# --------------------------------------------------------------- data model
@dataclass
class CourseState:
    course_id: int
    apply_weights: bool
    groups: list            # [{id, name, group_weight, rules}]
    assignments: list       # [{id, name, assignment_group_id, ...}]

    def group(self, gid):
        return next((g for g in self.groups if g["id"] == gid), None)


@dataclass
class Scheme:
    """The weighting to copy: ordered groups, and assignment-name -> group-name."""
    source_course_id: int
    groups: list            # [(name, weight, rules)]
    assignment_groups: dict  # norm(assignment name) -> group name

    @property
    def total(self):
        return sum(w for _, w, _ in self.groups)


@dataclass
class Change:
    op: str                 # create_group | move_assignment | set_weight | set_weighting
    what: str               # human description
    before: str
    after: str
    args: dict = field(default_factory=dict)

    def key(self):
        # Includes "before": if a course changed after the preview in ANY way the
        # teacher saw, the preview is stale - even if the end result would match.
        return (self.op, json.dumps(self.args, sort_keys=True), self.before, self.after)


@dataclass
class CoursePlan:
    target: Target
    changes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    skip: str | None = None

    @property
    def signature(self):
        return tuple(c.key() for c in self.changes)


@dataclass
class GradingPlan:
    action: str                       # "apply" or "restore"
    source_label: str
    builder: Callable                 # CourseState -> (desired, warnings); raises SkipCourse
    builders: dict                    # course_id -> builder (restore uses one per course)
    courses: list                     # [CoursePlan]

    @property
    def to_change(self):
        return [c for c in self.courses if not c.skip and c.changes]

    @property
    def total_changes(self):
        return sum(len(c.changes) for c in self.to_change)


@dataclass
class GradingResult:
    target: Target
    outcome: str        # applied | dry-run | no-change | skipped | failed | unknown | verify-failed | not-run
    message: str = ""
    changes_done: int = 0
    submissions_checked: int = 0
    snapshot_path: str | None = None


class SkipCourse(Exception):
    pass


@dataclass
class Desired:
    apply_weights: bool
    weights: dict           # existing group id -> weight
    creates: list           # [(name, weight)]
    moves: dict             # assignment id -> ("id", gid) | ("new", name)


# ----------------------------------------------------- reading course state
def read_state(client: SafeCanvasClient, course_id: int) -> CourseState:
    course = client.get_course(course_id)
    groups = client.list_assignment_groups(course_id)
    assignments = client.list_assignments(course_id)
    return CourseState(int(course_id), bool(course.get("apply_assignment_group_weights")),
                       [{"id": g["id"], "name": g.get("name", ""), "group_weight": float(g.get("group_weight") or 0),
                         "rules": g.get("rules") or {}} for g in groups],
                       assignments)


def submissions_fingerprint(submissions: list) -> tuple[str, int]:
    """Hash of every grade-relevant field of every submission (order independent)."""
    rows = []
    for s in submissions:
        row = {k: s.get(k) for k in SUBMISSION_FIELDS}
        row["attachments"] = sorted(a.get("id") for a in (s.get("attachments") or []))
        rows.append(json.dumps(row, sort_keys=True, default=str))
    rows.sort()
    return hashlib.sha256("\n".join(rows).encode()).hexdigest(), len(rows)


def assignments_fingerprint(assignments: list) -> dict:
    return {a["id"]: {k: a.get(k) for k in ASSIGNMENT_FIELDS} for a in assignments}


# --------------------------------------------------------- desired states
def scheme_from_state(state: CourseState) -> Scheme:
    if not state.apply_weights:
        raise PlanError("The source course does not use weighted assignment groups. In Canvas, open the "
                        "source course > Assignments > ⋮ > 'Assignment Groups Weight' and turn it on first.")
    names = [norm(g["name"]) for g in state.groups]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise PlanError(f"The source course has two groups with the same name ({', '.join(sorted(dupes))}). "
                        "Rename one in Canvas first.")
    by_id = {g["id"]: g for g in state.groups}
    amap, seen = {}, {}
    for a in state.assignments:
        n = norm(a.get("name"))
        seen[n] = seen.get(n, 0) + 1
        g = by_id.get(a.get("assignment_group_id"))
        if g:
            amap[n] = g["name"]
    for n, count in seen.items():          # ambiguous source names are not copied
        if count > 1:
            amap.pop(n, None)
    return Scheme(state.course_id, [(g["name"], float(g["group_weight"]), g["rules"]) for g in state.groups], amap)


def desired_from_scheme(state: CourseState, scheme: Scheme):
    warnings = []
    by_name: dict = {}
    for g in state.groups:
        by_name.setdefault(norm(g["name"]), []).append(g)
    weights, creates, final = {}, [], {}
    for name, w, rules in scheme.groups:
        matches = by_name.get(norm(name), [])
        if len(matches) > 1:
            raise SkipCourse(f"Two groups named '{name}' in this course - rename one in Canvas first.")
        if matches:
            g = matches[0]
            weights[g["id"]] = w
            final[g["id"]] = w
            if rules and (g.get("rules") or {}) != rules:
                warnings.append(f"Group '{name}' has drop rules in the source; rules are not copied.")
        else:
            creates.append((name, w))
            if rules:
                warnings.append(f"Group '{name}' has drop rules in the source; rules are not copied.")
    scheme_names = {norm(n) for n, _, _ in scheme.groups}
    extra = [g for g in state.groups if norm(g["name"]) not in scheme_names]
    for g in extra:
        final[g["id"]] = g["group_weight"]
    total = sum(final.values()) + sum(w for _, w in creates)
    if abs(total - scheme.total) > TOTAL_TOLERANCE:
        weighted = ", ".join(f"'{g['name']}' ({fmt_weight(g['group_weight'])}%)"
                             for g in extra if g["group_weight"] > 0)
        raise SkipCourse(f"Weights would total {fmt_weight(total)}% instead of {fmt_weight(scheme.total)}% "
                         f"because of group(s) not in the source: {weighted}. Set them to 0% or rename them "
                         "in Canvas, then preview again.")
    if abs(scheme.total - 100) > TOTAL_TOLERANCE:
        warnings.append(f"The source weights total {fmt_weight(scheme.total)}%, not 100%.")

    moves = {}
    name_count: dict = {}
    for a in state.assignments:
        name_count[norm(a.get("name"))] = name_count.get(norm(a.get("name")), 0) + 1
    dup_names = sorted({a.get("name") for a in state.assignments
                        if name_count[norm(a.get("name"))] > 1 and norm(a.get("name")) in scheme.assignment_groups})
    if dup_names:
        warnings.append("Not moved (more than one assignment with that name): " + ", ".join(dup_names))
    unmatched_zero = []
    created_ids = {norm(n): None for n, _ in creates}
    for a in state.assignments:
        n = norm(a.get("name"))
        if n not in scheme.assignment_groups:
            g = state.group(a.get("assignment_group_id"))
            if g and same_weight(final.get(g["id"], g["group_weight"]), 0):
                unmatched_zero.append(a.get("name"))
            continue
        if name_count[n] > 1:
            continue
        target_name = norm(scheme.assignment_groups[n])
        current = state.group(a.get("assignment_group_id"))
        if current and norm(current["name"]) == target_name and len(by_name.get(target_name, [])) == 1:
            continue
        if target_name in created_ids:
            moves[a["id"]] = ("new", scheme.assignment_groups[n])
        else:
            moves[a["id"]] = ("id", by_name[target_name][0]["id"])
    if unmatched_zero:
        warnings.append(f"{len(unmatched_zero)} assignment(s) not in the source stay in a 0% group and will "
                        "not count toward the total: " + ", ".join(unmatched_zero[:6])
                        + (" ..." if len(unmatched_zero) > 6 else ""))
    return Desired(True, weights, creates, moves), warnings


def desired_from_snapshot(state: CourseState, snap: dict):
    """Undo: put weights, group membership and the weighting switch back as they were."""
    warnings = []
    snap_groups = {g["id"]: g for g in snap["groups"]}
    current_ids = {g["id"] for g in state.groups}
    missing = [g["name"] for gid, g in snap_groups.items() if gid not in current_ids]
    if missing:
        raise SkipCourse("Group(s) deleted since the snapshot: " + ", ".join(missing) + ". Restore by hand.")
    weights = {}
    for g in state.groups:
        if g["id"] in snap_groups:
            weights[g["id"]] = float(snap_groups[g["id"]]["group_weight"])
        else:
            weights[g["id"]] = 0.0
            warnings.append(f"Group '{g['name']}' was created later; it is set to 0% (groups are never deleted).")
    snap_map = {a["id"]: a["assignment_group_id"] for a in snap["assignments"]}
    moves = {}
    for a in state.assignments:
        gid = snap_map.get(a["id"])
        if gid is not None and gid != a.get("assignment_group_id"):
            moves[a["id"]] = ("id", gid)
    return Desired(bool(snap["apply_weights"]), weights, [], moves), warnings


def changes_for(state: CourseState, d: Desired) -> list:
    """Smallest ordered list of edits that turns `state` into `d`.
    Order: create groups -> move assignments -> set weights -> weighting switch,
    so that weighting is switched on last, when everything is already in place."""
    out = []
    for name, w in d.creates:
        out.append(Change("create_group", f"Create group '{name}'", "-", f"{fmt_weight(w)}%",
                          {"name": name, "weight": float(w)}))
    gname = {g["id"]: g["name"] for g in state.groups}
    for a in sorted(state.assignments, key=lambda a: a["id"]):
        if a["id"] not in d.moves:
            continue
        kind, ref = d.moves[a["id"]]
        if kind == "id" and ref == a.get("assignment_group_id"):
            continue
        dest = gname.get(ref, "?") if kind == "id" else ref
        out.append(Change("move_assignment", f"Move '{a.get('name')}'",
                          gname.get(a.get("assignment_group_id"), "?"), dest,
                          {"assignment_id": a["id"], "to": [kind, ref]}))
    for g in state.groups:
        if g["id"] in d.weights and not same_weight(d.weights[g["id"]], g["group_weight"]):
            out.append(Change("set_weight", f"Weight of '{g['name']}'", f"{fmt_weight(g['group_weight'])}%",
                              f"{fmt_weight(d.weights[g['id']])}%",
                              {"group_id": g["id"], "weight": float(d.weights[g["id"]])}))
    if d.apply_weights != state.apply_weights:
        out.append(Change("set_weighting", "Weighted final grade", "on" if state.apply_weights else "off",
                          "on" if d.apply_weights else "off", {"on": d.apply_weights}))
    return out


# --------------------------------------------------------------- service
class GradingCenter:
    def __init__(self, client: SafeCanvasClient, ledger: Ledger, snapshot_dir: str | Path,
                 limits: Limits | None = None, clock=time.time, sleep=time.sleep):
        if client.mode != "grading":
            raise ValueError("GradingCenter needs a client created with mode='grading'")
        self.client = client
        self.ledger = ledger
        self.snapshot_dir = Path(snapshot_dir)
        self.limits = limits or GRADING_DEFAULTS
        self.limiter = RateLimiter(ledger, self.limits, clock=clock, sleep=sleep, channel="grading")

    # ------------------------------------------------------------ preview
    def read_scheme(self, source_course_id: int) -> tuple[Scheme, CourseState]:
        state = read_state(self.client, source_course_id)
        return scheme_from_state(state), state

    def _plan_course(self, target: Target, builder) -> CoursePlan:
        cp = CoursePlan(target)
        try:
            state = read_state(self.client, target.course_id)
            desired, cp.warnings = builder(state)
            cp.changes = changes_for(state, desired)
        except SkipCourse as e:
            cp.skip = str(e)
        except CanvasError as e:
            cp.skip = f"Could not read course: {e}"
        return cp

    def build_copy_plan(self, source_course_id: int, targets: list, source_label: str = "") -> GradingPlan:
        if not targets:
            raise PlanError("Choose at least one course to apply the weighting to.")
        scheme, _ = self.read_scheme(source_course_id)
        builder = lambda st: desired_from_scheme(st, scheme)
        courses, seen = [], set()
        for t in targets:
            if t.course_id in seen:
                continue
            seen.add(t.course_id)
            if t.course_id == int(source_course_id):
                courses.append(CoursePlan(t, skip="This is the source course"))
                continue
            courses.append(self._plan_course(t, builder))
        plan = GradingPlan("apply", source_label or f"course {source_course_id}", builder,
                           {c.target.course_id: builder for c in courses}, courses)
        self._check_size(plan)
        return plan

    def build_restore_plan(self, snapshot_path: str, target: Target) -> GradingPlan:
        snap = load_snapshot(snapshot_path)
        if int(snap["course_id"]) != target.course_id:
            raise PlanError("Snapshot belongs to a different course.")
        builder = lambda st: desired_from_snapshot(st, snap)
        cp = self._plan_course(target, builder)
        plan = GradingPlan("restore", f"snapshot {Path(snapshot_path).name}", builder,
                           {target.course_id: builder}, [cp])
        self._check_size(plan)
        return plan

    def _check_size(self, plan: GradingPlan):
        if plan.total_changes > self.limits.max_posts_per_run:
            raise PlanError(f"This would make {plan.total_changes} edits; the per-run limit is "
                            f"{self.limits.max_posts_per_run}. Select fewer courses.")

    # ------------------------------------------------------------ execute
    def execute(self, plan: GradingPlan, dry_run: bool = True,
                progress: Callable[[GradingResult], None] | None = None,
                cancel: threading.Event | None = None) -> list:
        emit = progress or (lambda r: None)
        results, stop = [], None
        self.limiter.start_run()
        for cp in plan.courses:
            t = cp.target
            if cp.skip:
                r = GradingResult(t, "skipped", cp.skip)
            elif stop or (cancel and cancel.is_set()):
                r = GradingResult(t, "not-run", stop or "Cancelled before this course")
            else:
                r = self._execute_course(plan, cp, dry_run)
                if r.outcome in ("failed", "unknown", "verify-failed"):
                    stop = f"Stopped because of the problem in {t.name}."
            results.append(r)
            emit(r)
        return results

    def _execute_course(self, plan: GradingPlan, cp: CoursePlan, dry_run: bool) -> GradingResult:
        t = cp.target
        builder = plan.builders[t.course_id]
        try:
            state = read_state(self.client, t.course_id)
            desired, _ = builder(state)
            changes = changes_for(state, desired)
        except SkipCourse as e:
            return GradingResult(t, "skipped", str(e))
        except CanvasError as e:
            return GradingResult(t, "skipped", f"Could not read course: {e}")
        if tuple(c.key() for c in changes) != cp.signature:
            return GradingResult(t, "skipped", "This course changed since the preview. Preview again.")
        if not changes:
            return GradingResult(t, "no-change", "Already matches - nothing to change")
        if dry_run:
            return GradingResult(t, "dry-run", f"Would make {len(changes)} change(s) (dry run - nothing changed)")

        # Safety net 1: snapshot (for undo) and grade fingerprint BEFORE any write.
        try:
            subs_before, n_subs = submissions_fingerprint(self.client.list_submissions(t.course_id))
        except CanvasError as e:
            return GradingResult(t, "skipped", f"Could not read submissions, so nothing was changed: {e}")
        assign_before = assignments_fingerprint(state.assignments)
        snap_path = save_snapshot(self.snapshot_dir, t, state)

        done, created = 0, {}
        try:
            for ch in changes:
                self.limiter.acquire()
                self._apply(t.course_id, ch, created)
                done += 1
        except RateLimitExceeded as e:
            return self._finish(plan, t, "failed", f"Stopped after {done} of {len(changes)} edits: {e}. "
                                "Preview again to finish, or Undo from History.", done, n_subs, snap_path)
        except CanvasError as e:
            outcome = "unknown" if e.ambiguous else "failed"
            msg = (f"Edit {done + 1} of {len(changes)} ({ch.what}) " +
                   ("may or may not have been applied" if e.ambiguous else "was refused") +
                   f": {e}. The {done} edit(s) before it were applied; nothing after it was tried. "
                   "Preview again to finish, or Undo from History.")
            return self._finish(plan, t, outcome, msg, done, n_subs, snap_path)

        # Safety net 2: re-read and verify everything.
        try:
            after = read_state(self.client, t.course_id)
            subs_after, n_after = submissions_fingerprint(self.client.list_submissions(t.course_id))
            desired_after, _ = builder(after)
            remaining = changes_for(after, desired_after)
        except (CanvasError, SkipCourse) as e:
            return self._finish(plan, t, "verify-failed", f"Changes made, but verification could not run: {e}",
                                done, n_subs, snap_path)
        problems = []
        if subs_after != subs_before or n_after != n_subs:
            problems.append("submissions/grades differ from before (was someone grading at the same time?)")
        if assignments_fingerprint(after.assignments) != assign_before:
            problems.append("assignment details differ from before")
        if remaining:
            problems.append(f"{len(remaining)} change(s) did not take effect")
        if problems:
            return self._finish(plan, t, "verify-failed", "VERIFY FAILED: " + "; ".join(problems) +
                                ". The run was stopped. Check this course in Canvas.", done, n_subs, snap_path)
        return self._finish(plan, t, "applied",
                            f"{done} change(s) applied. {n_subs} submission(s) verified unchanged.",
                            done, n_subs, snap_path)

    def _apply(self, course_id, ch: Change, created: dict):
        a = ch.args
        if ch.op == "create_group":
            g = self.client.create_assignment_group(course_id, a["name"], a["weight"])
            created[norm(a["name"])] = g["id"]
        elif ch.op == "move_assignment":
            kind, ref = a["to"]
            gid = ref if kind == "id" else created.get(norm(ref))
            if gid is None:
                raise CanvasError(f"New group '{ref}' was not created")
            self.client.move_assignment(course_id, a["assignment_id"], gid)
        elif ch.op == "set_weight":
            self.client.set_group_weight(course_id, a["group_id"], a["weight"])
        elif ch.op == "set_weighting":
            self.client.set_weighting(course_id, a["on"])
        else:
            raise ValueError(ch.op)

    def _finish(self, plan, t, outcome, msg, done, n_subs, snap_path):
        self.ledger.record_grading_run(plan.action, t.course_id, t.name, plan.source_label, outcome,
                                       done, n_subs, str(snap_path), msg)
        return GradingResult(t, outcome, msg, done, n_subs, str(snap_path))


# ------------------------------------------------------------- snapshots
def save_snapshot(directory: Path, target: Target, state: CourseState) -> Path:
    """Weighting setup only - group names/weights and which group each assignment
    is in. Contains no student data."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {
        "version": 1, "course_id": target.course_id, "course_name": target.name,
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "apply_weights": state.apply_weights,
        "groups": [{"id": g["id"], "name": g["name"], "group_weight": g["group_weight"]} for g in state.groups],
        "assignments": [{"id": a["id"], "name": a.get("name"), "assignment_group_id": a.get("assignment_group_id")}
                        for a in state.assignments],
    }
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
    path = directory / f"course{target.course_id}-{stamp}.json"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    return path


def load_snapshot(path) -> dict:
    with open(path) as f:
        data = json.load(f)
    for key in ("course_id", "apply_weights", "groups", "assignments"):
        if key not in data:
            raise PlanError(f"Snapshot {path} is missing '{key}'")
    return data
