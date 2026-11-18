"""Scripted end-to-end simulation: `python -m ccc simulate`.

Runs the real CommandCenter against the in-memory FakeCanvas and narrates each
step, including the duplicate-protection and failure scenarios.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .canvas_client import SafeCanvasClient
from .config import Limits
from .copier import CommandCenter, PlanError, Target
from .ledger import Ledger
from .simulator import SIM_TOKEN, SIM_URL, FakeCanvas


def _h(text):
    print(f"\n=== {text} " + "=" * max(0, 66 - len(text)))


def _plan(plan):
    print(f"  Item: [{plan.kind}] {plan.title!r}")
    for w in plan.warnings:
        print(f"  ! {w}")
    for r in plan.rows:
        extra = f"  ! {'; '.join(r.warnings)}" if r.warnings else ""
        print(f"   {r.action.upper():5} -> {r.target.name:<36} {r.reason}{extra}")


def _results(results):
    for r in results:
        cid = f" (new id {r.created_id})" if r.created_id else ""
        print(f"   {r.outcome.upper():8} -> {r.target.name:<36} {r.message}{cid}")


def run(verbose: bool = True) -> dict:
    fake = FakeCanvas.demo()
    before = fake.snapshot()
    tmp = tempfile.TemporaryDirectory()
    ledger = Ledger(Path(tmp.name) / "ledger.db")
    client = SafeCanvasClient(SIM_URL, SIM_TOKEN, transport=fake)
    cc = CommandCenter(client, ledger, Limits(min_seconds_between_posts=0.0), sleep=lambda s: None)

    _h("1. Connect and list the courses you teach")
    print(f"  Signed in as: {client.whoami()['name']}")
    courses = client.list_teacher_courses()
    for c in courses:
        print(f"   {c['id']}  {c['name']}  [{c['workflow_state']}]")
    targets = [Target(c["id"], c["name"], c["workflow_state"] == "available") for c in courses if c["id"] != 101]

    ann = next(a for a in client.list_announcements(101) if a["title"] == "Welcome to Week 12")
    _h("2. Preview: send announcement from Section A to all other sections")
    plan = cc.build_plan("announcement", 101, ann["id"], targets)
    _plan(plan)

    _h("3. Dry run (nothing is posted)")
    _results(cc.execute(plan, dry_run=True))
    print(f"  POSTs so far: {client.posts_sent}")

    _h("4. Real send")
    _results(cc.execute(plan, dry_run=False))
    for e in fake.emails:
        print(f"   EMAIL   -> course {e['course_id']}: {e['recipients']} students notified about {e['title']!r}")

    _h("5. Try to send the SAME announcement again (must be blocked)")
    plan2 = cc.build_plan("announcement", 101, ann["id"], targets)
    _plan(plan2)
    posts_before = client.posts_sent
    _results(cc.execute(plan2, dry_run=False))
    print(f"  New POSTs on second attempt: {client.posts_sent - posts_before}")

    _h("6. Re-run the OLD plan from step 2 (stale preview) - still blocked")
    posts_before = client.posts_sent
    _results(cc.execute(plan, dry_run=False))
    print(f"  New POSTs: {client.posts_sent - posts_before}")

    _h("7. Send an assignment (Section C already has one with that name)")
    hw = next(a for a in client.list_assignments(101) if a["name"].startswith("Homework 7"))
    plan3 = cc.build_plan("assignment", 101, hw["id"], targets, publish_assignment=True)
    _plan(plan3)
    _results(cc.execute(plan3, dry_run=False))

    _h("8. A quiz is refused (cannot be copied safely)")
    quiz = next(a for a in client.list_assignments(101) if a["name"] == "Quiz 5")
    try:
        cc.build_plan("assignment", 101, quiz["id"], targets)
    except PlanError as e:
        print(f"   REFUSED: {e}")

    _h("9. Network drops after Canvas created the item (worst case)")
    lab = next(a for a in client.list_assignments(101) if a["name"] == "Lab Report 3")
    plan4 = cc.build_plan("assignment", 101, lab["id"], targets)
    fake.fail_next_post = "timeout"
    _results(cc.execute(plan4, dry_run=False))
    print("  -> The tool stopped instead of retrying. The ledger keeps it 'pending':")
    _plan(cc.build_plan("assignment", 101, lab["id"], targets))

    _h("10. Rate limit: per-run cap of 2")
    small = CommandCenter(client, ledger, Limits(min_seconds_between_posts=0, max_posts_per_run=2),
                          sleep=lambda s: None)
    ann2 = next(a for a in client.list_announcements(101) if a["title"] == "Midterm grades posted")
    try:
        small.build_plan("announcement", 101, ann2["id"], targets)
    except PlanError as e:
        print(f"   REFUSED at preview: {e}")

    _h("11. Safety audit")
    methods = sorted({m for m, _ in fake.requests})
    posts = [p for m, p in fake.requests if m == "POST"]
    unchanged = all(
        any(i == orig for i in fake.courses[cid][coll])
        for cid, c in before.items() for coll in ("announcements", "assignments") for orig in c[coll])
    print(f"   HTTP methods used         : {', '.join(methods)}")
    print(f"   Total POSTs (all creates) : {len(posts)}")
    print(f"   Pre-existing items intact : {unchanged}")
    per_target = {}
    for row in ledger.all_rows():
        per_target.setdefault((row.kind, row.source_item, row.target_course), 0)
        per_target[(row.kind, row.source_item, row.target_course)] += 1
    print(f"   Max copies of any item in any course: {max(per_target.values())}")
    print(f"   Emails sent (simulated)   : {sum(e['recipients'] for e in fake.emails)} "
          f"across {len(fake.emails)} courses")
    ledger.close()
    tmp.cleanup()
    return {"methods": methods, "posts": len(posts), "unchanged": unchanged}




def run_grading() -> dict:
    """Narrated grading-weighting simulation: `python -m ccc simulate-grading`."""
    from .grading import GradingCenter
    fake = FakeCanvas.demo()
    before = fake.snapshot()
    tmp = tempfile.TemporaryDirectory()
    ledger = Ledger(Path(tmp.name) / "ledger.db")
    client = SafeCanvasClient(SIM_URL, SIM_TOKEN, transport=fake, mode="grading")
    gc = GradingCenter(client, ledger, Path(tmp.name) / "snapshots",
                       Limits(min_seconds_between_posts=0, max_posts_per_run=400, max_posts_per_hour=1000),
                       sleep=lambda s: None)
    targets = [Target(c, fake.courses[c]["name"], fake.courses[c]["workflow_state"] == "available")
               for c in (102, 103, 104, 105)]

    def show(plan):
        for cp in plan.courses:
            head = f"SKIP  {cp.skip}" if cp.skip else (f"{len(cp.changes)} change(s)" if cp.changes else "already matches")
            print(f"   {cp.target.name:<36} {head}")
            for ch in cp.changes:
                print(f"       - {ch.what:<44} {ch.before:>12} -> {ch.after}")
            for w in cp.warnings:
                print(f"       ! {w}")

    def results(rs):
        for r in rs:
            print(f"   {r.outcome.upper():<13} {r.target.name:<36} {r.message}")

    _h("1. The weighting set up once in Section A (the source)")
    scheme, state = gc.read_scheme(101)
    for name, w, _ in scheme.groups:
        print(f"   {name:<14} {w:>5g}%")

    _h("2. Preview copying it to Sections B-E (read-only)")
    plan = gc.build_copy_plan(101, targets, "MA 121 - Section A")
    show(plan)
    print(f"   Edits sent to Canvas so far: {len(fake.writes)}")

    _h("3. Apply for real")
    results(gc.execute(plan, dry_run=False))

    _h("4. Preview again - nothing left to do")
    print(f"   Changes needed now: {gc.build_copy_plan(101, targets).total_changes}")

    _h("5. Undo Section B from its snapshot")
    run = next(r for r in ledger.grading_runs() if r.course_id == 102)
    rplan = gc.build_restore_plan(run.snapshot_path, targets[0])
    show(rplan)
    results(gc.execute(rplan, dry_run=False))

    _h("6. A refused edit (closed grading period) stops the run safely")
    fresh = FakeCanvas.demo()
    lab = next(a for a in fresh.courses[102]["assignments"] if a["name"] == "Lab Report 1")
    fresh.locked_assignments.add(lab["id"])
    gc2 = GradingCenter(SafeCanvasClient(SIM_URL, SIM_TOKEN, transport=fresh, mode="grading"), ledger,
                        Path(tmp.name) / "snapshots2", gc.limits, sleep=lambda s: None)
    results(gc2.execute(gc2.build_copy_plan(101, targets[:2]), dry_run=False))

    _h("7. Safety audit")
    subs_same = all(before[c]["submissions"] == fake.courses[c]["submissions"] for c in before)
    others_same = all({k: v for k, v in a.items() if k != "assignment_group_id"} ==
                      {k: v for k, v in b.items() if k != "assignment_group_id"}
                      for c in before for a, b in zip(before[c]["assignments"], fake.courses[c]["assignments"]))
    kinds = sorted({(m, re.sub(r"/\d+", "/<id>", p), tuple(sorted(f))) for m, p, f in fake.writes})
    print(f"   Submissions & grades identical in every course : {subs_same}")
    print(f"   Assignment details identical (except group)    : {others_same}")
    print(f"   Kinds of edits sent:")
    for m, p, f in kinds:
        print(f"      {m:<4} {p:<45} fields {list(f)}")
    ledger.close()
    tmp.cleanup()
    return {"submissions_same": subs_same, "others_same": others_same}


if __name__ == "__main__":
    run()
